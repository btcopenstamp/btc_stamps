import hashlib
import json
import logging
import re
import sys
import time
from collections import defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Dict, List, Optional, TypedDict, Union

import index_core.log as log
import requests
from config import (  # SRC_VALIDATION_API1,
    CP_P2WSH_FEAT_BLOCK_START,
    SRC20_BALANCES_TABLE,
    SRC20_VALID_TABLE,
    SRC_VALIDATION_API2,
    SRC_VALIDATION_SECRET_API2,
    TICK_PATTERN_SET,
)
from index_core.database import TOTAL_MINTED_CACHE, get_src20_deploy, get_srcbackground_data, get_total_src20_minted_from_db
from index_core.util import decode_unicode_escapes, escape_non_ascii_characters
from requests.exceptions import JSONDecodeError

D = Decimal
logger = logging.getLogger(__name__)
log.set_logger(logger)


class Src20Dict(TypedDict, total=False):
    tick: Optional[str]
    p: Optional[str]
    op: Optional[str]
    holders_of: Optional[str]
    max: Optional[Union[str, D]]
    lim: Optional[Union[str, D]]
    amt: Optional[Union[str, D]]
    dec: Optional[Union[str, int]]
    status: Optional[str]
    tick_hash: Optional[str]


class Src20Validator:
    @property
    def errors(self):
        """
        Returns the list of validation errors.
        """
        return self.validation_errors

    def __init__(self, src20_dict: Src20Dict):
        self.src20_dict = src20_dict
        self.validation_errors: List[str] = []

    def process_values(self) -> Src20Dict:
        num_pattern = re.compile(r"^[0-9]*(\.[0-9]*)?$")
        dec_pattern = re.compile(r"^[0-9]+$")

        for key, value in list(self.src20_dict.items()):
            if value == "":
                self.src20_dict[key] = None  # type: ignore
            elif key in ["tick"]:
                self._process_tick_value(key, value)  # type: ignore
            elif key in ["p", "op", "holders_of"]:
                self._process_uppercase_value(key, value)  # type: ignore
            elif key in ["max", "lim", "amt", "dec"]:
                self._apply_regex_validation(key, value, num_pattern, dec_pattern)

        return self.src20_dict

    def _apply_regex_validation(self, key, value, num_pattern, dec_pattern):
        if key in ["max", "lim", "amt"]:
            if isinstance(value, D):
                self.src20_dict[key] = value
            elif num_pattern.match(str(value)):
                self.src20_dict[key] = D(str(value))
            else:
                self._update_status(key, f"NN: INVALID NUM for {key}")
                self.src20_dict[key] = None
            if key in ["max", "lim"] and self.src20_dict[key] is not None:
                self.src20_dict[key] = self.src20_dict[key].quantize(Decimal('1'), rounding=ROUND_DOWN)
        elif key == "dec":
            if dec_pattern.match(str(value)) and 0 <= int(value) <= 18:
                self.src20_dict[key] = int(value)
            else:
                self._update_status(key, f"NN: INVALID DEC VAL {key}")
                self.src20_dict[key] = None

    def _update_status(self, key: str, message: str) -> None:
        error_message = f"{key}: {message}"
        self.validation_errors.append(error_message)

        if "status" in self.src20_dict and self.src20_dict["status"] is not None:
            self.src20_dict["status"] += f", {error_message}"
        else:
            self.src20_dict["status"] = error_message

    def _process_tick_value(self, key: str, value: str) -> None:
        self.src20_dict["tick"] = value.lower()
        self.src20_dict["tick"] = escape_non_ascii_characters(self.src20_dict["tick"])
        self.src20_dict["tick_hash"] = self.create_tick_hash(value.lower())

    def _process_uppercase_value(self, key: str, value: str) -> None:
        self.src20_dict[key] = value.upper()  # type: ignore

    @staticmethod
    def create_tick_hash(tick: str) -> str:
        """
        Create a SHA3-256 of the normalized tick value. This is the final NIST SHA3-256 implementation
        not to be confused with Keccak-256 which is the Ethereum implementation of SHA3-256.
        """
        return hashlib.sha3_256(tick.encode()).hexdigest()

    @property
    def is_valid(self):
        return len(self.validation_errors) == 0


class Src20Processor:
    STATUS_MESSAGES = {  # second value in tuple  = is_invalid
        "DE": ("INVALID DEPLOY: {tick} DEPLOY EXISTS", True),
        "ND": ("INVALID {op}: {tick} NO DEPLOY", True),
        "OM": ("OVER MINT {tick} {total_minted} >= {deploy_max}", True),
        "NA": ("INVALID AMT {op} {tick}", True),
        "OMA": ("REDUCED AMT {tick} FROM:  {original_amt} TO: {adjusted_amt}", False),
        "ODL": ("REDUCED AMT {tick} FROM:  {original_amt} TO: {adjusted_amt}", False),
        "BB": (
            "INVALID XFR {tick} - total_balance {balance} < xfer amt {amount}",
            True,
        ),
        "UO": ("UNSUPPORTED OP {op} ", True),
        "ID": ("INVALID DECIMAL {tick} - decimal len {dec_length} > {dec}", True),
    }

    def __init__(self, db, src20_dict, processed_src20_in_block):
        self.db = db
        self.src20_dict = src20_dict
        self.processed_src20_in_block = processed_src20_in_block
        self.is_valid = True
        self.dec: Optional[Union[str, int]] = src20_dict.get("dec", 0)
        self.deploy_lim: Optional[Union[str, D]] = src20_dict.get("deploy_lim", 0)
        self.deploy_max: Optional[Union[str, D]] = src20_dict.get("deploy_max", 0)

    def normalize_and_validate_amt(self):
        amt = D(self.src20_dict["amt"]).normalize()
        self.dec = int(self.dec) if self.dec is not None else 18
        decimal_length = -int(amt.as_tuple().exponent)

        if decimal_length > self.dec:
            self.decimal_length = decimal_length
            raise ValueError("Decimal places exceeds the limit")
        else:
            self.src20_dict["amt"] = amt
            self.src20_dict["dec"] = self.dec
            return amt

    def update_valid_src20_list(
        self,
        running_user_balance_creator=None,
        running_user_balance_destination=None,
        operation=None,
        total_minted=None,
    ):
        if operation == "TRANSFER":
            try:
                amt = self.normalize_and_validate_amt()
            except ValueError:
                self.set_status_and_log(
                    "ID",
                    tick=self.src20_dict["tick"],
                    dec_length=self.decimal_length,
                    dec=self.dec,
                )
                return
            self.src20_dict["total_balance_creator"] = D(running_user_balance_creator) - amt
            self.src20_dict["total_balance_destination"] = D(running_user_balance_destination) + amt
            # self.src20_dict['status'] = 'Balance Updated'
        elif operation == "MINT" and total_minted is not None:
            try:
                amt = self.normalize_and_validate_amt()
            except ValueError:
                self.set_status_and_log(
                    "ID",
                    tick=self.src20_dict["tick"],
                    dec_length=self.decimal_length,
                    dec=self.dec,
                )
                return
            TOTAL_MINTED_CACHE[self.src20_dict.get("tick")] += amt
            running_total_mint = D(total_minted) + amt
            running_user_balance = D(running_user_balance_creator) + amt
            self.src20_dict["total_minted"] = running_total_mint
            self.src20_dict["total_balance_destination"] = running_user_balance
        elif operation == "DEPLOY":
            if self.src20_dict.get("dec") is None:
                self.src20_dict["dec"] = 18
        else:
            raise Exception(f"Invalid Operation '{operation}' on SRC20 Table Insert")

        self.src20_dict["valid"] = 1

    def create_running_user_balance_dict(self, running_user_balance_tuple):
        running_user_balance_dict = {}

        for balance_tuple in running_user_balance_tuple:
            address = getattr(balance_tuple, "address")
            total_balance = getattr(balance_tuple, "total_balance")
            running_user_balance_dict[address] = total_balance

        return running_user_balance_dict

    def set_status_and_log(self, status_code: str, **kwargs):
        message_template, is_invalid = self.STATUS_MESSAGES[status_code]
        message = message_template.format(**kwargs)
        status_message = f"{status_code}: {message}"
        self.src20_dict["status"] = status_message

        if is_invalid:
            logger.warning(message)
            self.is_valid = False
        else:
            logger.info(message)

    def handle_deploy(self):
        if self.operation.upper() != "DEPLOY":
            logger.warning(f"Attempted to handle non-DEPLOY operation: {self.operation} for tick: {self.src20_dict['tick']}")
            return

        if not self.deploy_lim and not self.deploy_max:
            self.update_valid_src20_list(operation=self.operation)

            # Extract metadata from the SRC20 DEPLOY json string
            metadata = {
                "tick": self.src20_dict["tick"],
                "tick_hash": self.src20_dict["tick_hash"],
                "description": self.src20_dict.get("desc"),
                "x": self.src20_dict.get("x"),
                "tg": self.src20_dict.get("tg"),
                "web": self.src20_dict.get("web"),
                "email": self.src20_dict.get("email"),
                "deploy_block_index": self.src20_dict["block_index"],
                "deploy_tx_hash": self.src20_dict["tx_hash"],
            }

            self.insert_src20_metadata(metadata)
        else:
            self.set_status_and_log("DE", tick=self.src20_dict["tick"])

    def insert_src20_metadata(self, metadata):
        with self.db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO src20_metadata
                (tick, tick_hash, description, x, tg, web, email, deploy_block_index, deploy_tx_hash)
                VALUES (%(tick)s, %(tick_hash)s, %(description)s, %(x)s, %(tg)s, %(web)s, %(email)s, %(deploy_block_index)s, %(deploy_tx_hash)s)
                ON DUPLICATE KEY UPDATE
                description = COALESCE(VALUES(description), description),
                x = COALESCE(VALUES(x), x),
                tg = COALESCE(VALUES(tg), tg),
                web = COALESCE(VALUES(web), web),
                email = COALESCE(VALUES(email), email)
                """,
                metadata,
            )

    def handle_mint(self):
        self.deploy_lim = min(D(self.deploy_lim), D(self.deploy_max)) if self.deploy_lim and self.deploy_max else D(0)

        try:
            total_minted = D(get_running_mint_total(self.db, self.processed_src20_in_block, self.src20_dict["tick"]))
            mint_available = D(self.deploy_max) - total_minted if self.deploy_max else D(0)

            if total_minted >= D(self.deploy_max) if self.deploy_max else D(0):
                self.set_status_and_log(
                    "OM",
                    total_minted=total_minted,
                    deploy_max=self.deploy_max,
                    tick=self.src20_dict["tick"],
                )
                return

            if self.src20_dict["amt"] > mint_available:
                self.set_status_and_log(
                    "OMA",
                    original_amt=self.src20_dict["amt"],
                    adjusted_amt=mint_available,
                    tick=self.src20_dict["tick"],
                )
                self.src20_dict["amt"] = mint_available

            if self.src20_dict["amt"] > self.deploy_lim:
                self.set_status_and_log(
                    "ODL",
                    original_amt=self.src20_dict["amt"],
                    adjusted_amt=self.deploy_lim,
                    tick=self.src20_dict["tick"],
                )
                self.src20_dict["amt"] = self.deploy_lim

            running_user_balance = D("0")
            running_user_balance_tuple = get_running_user_balances(
                self.db,
                self.src20_dict["tick"],
                self.src20_dict["tick_hash"],
                self.src20_dict["destination"],
                self.processed_src20_in_block,
            )
            if running_user_balance_tuple:
                running_user_balance = running_user_balance_tuple[0].total_balance

            self.update_valid_src20_list(
                running_user_balance_creator=running_user_balance,
                operation=self.operation,
                total_minted=total_minted,
            )

        except Exception as e:
            logger.error(f"Error in minting operations: {e}")
            raise

    def handle_transfer(self):
        try:
            if self.src20_dict["creator"] == self.src20_dict["destination"]:
                running_user_balance_tuple = get_running_user_balances(
                    self.db,
                    self.src20_dict["tick"],
                    self.src20_dict["tick_hash"],
                    [self.src20_dict["creator"]],
                    self.processed_src20_in_block,
                )
            else:
                addresses = [self.src20_dict["creator"], self.src20_dict["destination"]]
                running_user_balance_tuple = get_running_user_balances(
                    self.db,
                    self.src20_dict["tick"],
                    self.src20_dict["tick_hash"],
                    addresses,
                    self.processed_src20_in_block,
                )
            running_user_balance_dict = self.create_running_user_balance_dict(running_user_balance_tuple)

            running_user_balance_creator = D(running_user_balance_dict.get(self.src20_dict["creator"], 0))
            running_user_balance_destination = D(running_user_balance_dict.get(self.src20_dict["destination"], 0))

            # Check if the creator has enough balance to transfer
            if running_user_balance_creator < D(self.src20_dict["amt"]):
                self.set_status_and_log(
                    "BB",
                    balance=running_user_balance_creator,
                    amount=self.src20_dict["amt"],
                    tick=self.src20_dict["tick"],
                )
                return

            self.update_valid_src20_list(
                running_user_balance_creator,
                running_user_balance_destination,
                operation="TRANSFER",
            )

        except Exception as e:
            logger.error(f"Error in handle_transfer: {e}")
            raise

    def handle_bulk_transfer(
        self,
    ):  # NOTE: this is not yet implemented on a block height activation or in the operation handling
        # Check if operation is BULK_XFER and if deploy limits are set
        if self.src20_dict["op"] != "BULK_XFER" or not (self.deploy_lim and self.deploy_max):
            logger.info(f"Invalid {self.src20_dict['tick']} BULK_XFER - deployment limits not set or operation mismatch")
            return

        # Validate the 'holders_of' target deploy
        target_lim, target_max, dec = get_src20_deploy(self.db, self.src20_dict["holders_of"], self.processed_src20_in_block)
        if not (target_lim and target_max):
            self.set_status_and_log(
                "DD",
                f"Invalid {self.src20_dict['holders_of']} AD - Invalid holders_of",
                is_invalid=True,
            )
            return

        # Validate 'destinations' is a list
        if not isinstance(self.src20_dict["destinations"], list):
            logger.warning(f"Invalid {self.src20_dict['tick']} BULK_XFER - destinations not a list")
            return

        addresses = [self.src20_dict["creator"]]
        if self.src20_dict["creator"] != self.src20_dict["destination"]:
            addresses.append(self.src20_dict["destination"])

        running_user_balance_tuple = get_running_user_balances(
            self.db,
            self.src20_dict["tick"],
            self.src20_dict["tick_hash"],
            addresses,
            self.processed_src20_in_block,
        )
        running_user_balance_creator = getattr(running_user_balance_tuple, "total_balance", D("0"))

        if running_user_balance_creator <= 0:
            logger.info(f"Invalid {self.src20_dict['tick']} BULK_XFER - insufficient balance")
            return

        # Get tick holders and calculate total send amount
        tick_holders = get_tick_holders_from_balances(self.db, self.src20_dict["holders_of"])
        tick_holders.remove(self.src20_dict["creator"])  # Remove the creator from the target list
        total_send_amt = len(tick_holders) * D(self.src20_dict["amt"])

        if D(total_send_amt) > D(running_user_balance_creator):
            self.src20_dict["status"] = "BB: BULK_XFER over user balance"
            self.set_status_and_log(
                "BB",
                op="BULK_XFER",
                balance=running_user_balance_creator,
                amount=total_send_amt,
                tick=self.src20_dict["tick"],
            )
            return

        # Prepare transactions for each tick holder
        new_dicts = []
        running_dest_balances_tuple = get_running_user_balances(
            self.db,
            self.src20_dict["tick"],
            self.src20_dict["tick_hash"],
            tick_holders,
            self.processed_src20_in_block,
        )
        running_dest_balance_dict = self.create_running_user_balance_dict(running_dest_balances_tuple)

        new_dicts = [
            {
                **self.src20_dict,
                "op": "TRANSFER",
                "destination": th,
                "total_balance_destination": running_dest_balance_dict.get(th, D("0")) + D(self.src20_dict["amt"]),
            }
            for th in tick_holders
        ]

        self.processed_src20_in_block.extend(new_dicts)
        self.src20_dict["total_balance_creator"] = D(running_user_balance_creator) - D(total_send_amt)
        self.src20_dict["status"] = f'New Balance: {self.src20_dict["total_balance_creator"]}'

    def validate_and_process_operation(self):
        self.operation = self.src20_dict.get("op", "").upper()
        op_amt_validations = ["TRANSFER", "MINT"]

        if self.operation in op_amt_validations and not self.src20_dict.get("amt"):
            self.set_status_and_log("NA", op=self.operation, tick=self.src20_dict["tick"])
            return

        self.deploy_lim, self.deploy_max, self.dec = get_src20_deploy(self.db, self.tick_value, self.processed_src20_in_block)

        if not self.deploy_lim and not self.deploy_max and self.operation in op_amt_validations:
            self.set_status_and_log("ND", op=self.operation, tick=self.src20_dict["tick"])
            return

        if self.operation == "DEPLOY":
            self.handle_deploy()
        elif self.operation == "MINT":
            self.handle_mint()
        elif self.operation == "TRANSFER":
            self.handle_transfer()
        else:
            self.set_status_and_log("UO", op=self.operation, tick=self.src20_dict.get("tick", "undefined"))

    def process(self):
        validator = Src20Validator(self.src20_dict)
        self.src20_dict = validator.process_values()
        self.tick_value = self.src20_dict.get("tick")

        if not validator.is_valid:
            self.processed_src20_in_block.append(self.src20_dict)
            logger.warning(f"Invalid {self.tick_value} SRC20: {self.src20_dict['status']}")
            self.is_valid = False
            return

        self.validate_and_process_operation()


def parse_src20(db, src20_dict, processed_src20_in_block):
    """
    Process all SRC-20 tokens that pass check_format.
    """
    processor = Src20Processor(db, src20_dict, processed_src20_in_block)
    processor.process()
    return processor.is_valid, src20_dict


def build_src20_svg_string(db, src_20_dict):
    background_base64, font_size, text_color = get_srcbackground_data(db, src_20_dict.get("tick"))
    svg_image_data = generate_srcbackground_svg(src_20_dict, background_base64, font_size, text_color)
    return svg_image_data


def format_address(address):
    return address[:4] + "..." + address[-4:]


def generate_srcbackground_svg(input_dict, base64, font_size, text_color):
    if "\\" in input_dict["tick"]:
        input_dict["tick"] = decode_unicode_escapes(input_dict["tick"])

    dict_to_use = {}

    if input_dict.get("op").upper() == "DEPLOY":
        dict_to_use = {
            "p": input_dict.get("p", None).upper(),
            "op": input_dict.get("op", None).upper(),
            "tick": input_dict.get("tick", None).upper(),
            "max": input_dict.get("max", None),
            "lim": input_dict.get("lim", None),
        }
    elif input_dict.get("op").upper() == "MINT":
        dict_to_use = {
            "p": input_dict.get("p", None).upper(),
            "op": input_dict.get("op", None).upper(),
            "tick": input_dict.get("tick", None).upper(),
            "amt": input_dict.get("amt", None),
        }
    elif input_dict.get("op").upper() == "TRANSFER":
        dict_to_use = {
            "p": input_dict.get("p", None).upper(),
            "op": input_dict.get("op", None).upper(),
            "tick": input_dict.get("tick", None).upper(),
            "amt": input_dict.get("amt", None),
        }
    if dict_to_use == {}:
        logger.log(
            logging.ERROR,
            "dict_to_use is empty -- happens with invalid op value but a valid stamp",
        )  # FIXME: process svg string after validation

    sorted_keys = sorted(dict_to_use.keys(), key=sort_keys)
    pretty_json = json.dumps(
        {k: dict_to_use[k] for k in sorted_keys},
        indent=1,
        separators=(",", ": "),
        sort_keys=False,
        ensure_ascii=False,
        default=str,
    )

    if base64 is not None:
        svg_output = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 420 420"><foreignObject font-size="{font_size}" width="100%" height="100%"><p xmlns="http://www.w3.org/1999/xhtml" style="background-image: url(data:{base64});color:{text_color};padding:20px;margin:0px;width:1000px;height:1000px;"><pre>{pretty_json}</pre></p></foreignObject></svg>"""
    else:
        svg_output = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 420 420"><foreignObject font-size="30px" width="100%" height="100%"><p xmlns="http://www.w3.org/1999/xhtml" style="background: rgb(149,56,182); background: linear-gradient(138deg, rgba(149,56,182,1) 23%, rgba(0,56,255,1) 100%);padding:20px;margin:0px;width:1000px;height:1000px;"><pre>{pretty_json}</pre></p></foreignObject></svg>"""
    img_data = svg_output.encode("utf-8")

    return img_data


def matches_any_pattern(text, char_set):
    """
    Checks if the characters in the given text matches chars in the pattern list.

    Args:
        text (str): The text to be checked.
        pattern_list (list): A list of regex patterns to match against.

    Returns:
        bool: True if all characters in the text matches the pattern list, False otherwise.
    """
    for char in text:
        if char not in char_set:
            return False
    return True


def sort_keys(key):
    priority_keys = ["p", "op", "tick"]
    if key in priority_keys:
        return priority_keys.index(key)
    return len(priority_keys)


def convert_to_utf8_string(tick_value):
    """
    Converts the tick value to a UTF-8 encoded string.

    Args:
        tick_value (str): The tick value to be converted.

    Returns:
        str: The converted tick value as a UTF-8 encoded string.
    """
    try:
        # This will work if tick_value is a string representation of a bytestring
        tick_value = tick_value.encode("latin-1").decode("utf-8")
    except UnicodeEncodeError:
        # This will work if tick_value is a valid UTF-8 character or a combination of ASCII and UTF-8 characters
        tick_value = tick_value.encode("utf-8").decode("utf-8")
    return tick_value


def check_format(input_string, tx_hash, block_index):
    """
    Check the format of the SRC-20 JSON string and return a dictionary if it meets the validation requirements.
    This function determines inclusion/exclusion as a valid stamp without affecting stamp numbering.

    It uses a custom parse_float function to detect and reject numbers in scientific notation before converting them to Decimal.
    Super ugly, but a consensus item for stamp numbering.

    Args:
        input_string (str or bytes or dict): The input string to be checked.
        tx_hash (str): The transaction hash associated with the input string.

    Returns:
        dict or None: If the input string meets the requirements for SRC-20, a dictionary representing the input string is returned.
                      Otherwise, None is returned.

    Raises:
        json.JSONDecodeError: If the input string cannot be decoded as JSON.
    """

    def parse_no_sci_float(s):
        if "e" in s.lower():
            logger.warning(f"EXCLUSION: Scientific notation not allowed in incoming value: {s}")
            raise ValueError(f"Scientific notation not allowed in incoming value: {s}")
        return D(s)

    try:
        try:
            if isinstance(input_string, bytes):
                input_string = input_string.decode("utf-8")
                input_dict = json.loads(input_string, parse_float=parse_no_sci_float, parse_int=D)
            elif isinstance(input_string, str):
                input_dict = json.loads(input_string, parse_float=parse_no_sci_float, parse_int=D)
            elif isinstance(input_string, dict):
                input_dict = input_string
            else:
                logger.warning("EXCLUSION: Input string is neither bytes, str, nor dict")
                return None
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"EXCLUSION: JSON decode error: {e}")
            return None
        if input_dict.get("p").lower() == "src-721":
            return input_dict
        elif input_dict.get("p").lower() == "src-20":
            tick_value = convert_to_utf8_string(input_dict.get("tick", ""))
            input_dict["tick"] = tick_value
            if not tick_value or not matches_any_pattern(tick_value, TICK_PATTERN_SET) or len(tick_value) > 5:
                logger.warning(f"EXCLUSION: did not match tick pattern {input_dict}")
                return None

            deploy_keys = {"op", "tick", "max", "lim"}
            transfer_keys = {"op", "tick", "amt"}
            mint_keys = {"op", "tick", "amt"}
            bulk_xfer_keys = {"op", "tick", "amt", "destinations"}

            input_keys = set(input_dict.keys())

            uint64_max = D(2**64 - 1)
            key_sets = [deploy_keys, transfer_keys, mint_keys, bulk_xfer_keys]
            key_values_to_check = {
                "deploy_keys": ["max", "lim"],
                "transfer_keys": ["amt"],
                "mint_keys": ["amt"],
                "bulk_xfer_keys": ["amt"],
            }

            for i, key_set in enumerate(key_sets):
                if input_keys >= key_set:
                    for key in key_values_to_check[list(key_values_to_check.keys())[i]]:
                        value = input_dict.get(key)
                        if value is None:
                            logger.warning(
                                f"EXCLUSION: Missing or invalid value for {key}",
                                input_dict,
                            )
                            return None

                        if isinstance(value, str):
                            try:
                                if block_index >= CP_P2WSH_FEAT_BLOCK_START:
                                    value = D(value) if value else D(0)
                                else:
                                    value = D("".join(c for c in value if c.isdigit() or c == ".")) if value else D(0)
                            except InvalidOperation as e:
                                logger.warning(
                                    f"EXCLUSION: {key} not a valid decimal: {e}. Input dict: {input_dict}, {tx_hash}"
                                )
                                return None
                        elif isinstance(value, int):
                            value = D(value)
                        elif isinstance(value, float):
                            value_str = format(value, "f")
                            value = D(value_str)
                        elif isinstance(value, D):
                            pass
                        else:
                            logger.warning(f"EXCLUSION: {key} not a string or integer", input_dict)
                            return None
                        if value.is_nan() or not (D("0") <= value <= uint64_max):
                            logger.warning(f"EXCLUSION: {key} not in range", input_dict)
                            return None
            return input_dict

    except json.JSONDecodeError:
        return None

    return None


def get_running_mint_total(db, src20_processed_in_block, tick):
    """
    Get the running mint total for a given tick.

    Args:
        db (Database): The database object.
        src20_processed_in_block (list): The list of processed SRC20 items in a block.
        tick (int): The tick value.

    Returns:
        Decimal: The running mint total for the given tick.
    """
    total_minted = 0
    if len(src20_processed_in_block) > 0:
        for item in reversed(src20_processed_in_block):
            if item["tick"] == tick and item["op"] == "MINT" and "total_minted" in item:
                total_minted = item["total_minted"]
                break
    if total_minted == 0:
        total_minted = get_total_src20_minted_from_db(db, tick)

    return D(total_minted)


def get_running_user_balances(db, tick, tick_hash, addresses, src20_processed_in_block):
    """
    Calculate the running balance of multiple users based on the processed transactions
    in current and prior blocks from the db. this is only be called once for each mint
    bulk_xfer, or transfer transaction it may get many addresses from the bulk_xfer list. The
    bulk_xfer list is assumed to have only unique addresses.

    Parameters:
    - db (Database): The database object.
    - tick (int): The tick value.
    - tick_hash (str): The tick hash value.
    - addresses (list or str): The list or string of addresses to calculate the balances for.
    - src20_processed_in_block (list): The list of already processed src20 transactions in the block.

    Returns:
    - list: A list of namedtuples containing the tick, address, and total balance for each address.
    """

    BalanceCurrent = namedtuple("BalanceCurrent", ["tick", "address", "total_balance", "locked_balance"])

    if isinstance(addresses, str):
        addresses = [addresses]
    if len(addresses) != len(set(addresses)):
        raise Exception(f"The addresses list is not all unique addresses: tick={tick}, addresses={addresses}")

    balances = []

    if any(item["tick"] == tick for item in src20_processed_in_block):
        try:
            for prior_tx in reversed(
                src20_processed_in_block
            ):  # if there is a total-balance in a trx in the block with the same address, tick, and tick_hash, use that value for total_balance_x
                if prior_tx.get("valid") == 1:  # Check if the dict has a valid key with a value of 1
                    for address in addresses:
                        total_balance = None
                        locked_balance = None
                        if (
                            prior_tx["creator"] == address
                            and prior_tx["tick"] == tick
                            and prior_tx["tick_hash"] == tick_hash
                            and "total_balance_creator"
                            in prior_tx  # this gets added to the tuple which will be returned for the address and later added to src20_valid.??
                        ):
                            if "total_balance_creator" in prior_tx:
                                total_balance = prior_tx["total_balance_creator"]

                        elif (
                            prior_tx["destination"] == address
                            and prior_tx["tick"] == tick
                            and prior_tx["tick_hash"] == tick_hash
                            and "total_balance_destination" in prior_tx
                        ):
                            if "total_balance_destination" in prior_tx:
                                total_balance = prior_tx["total_balance_destination"]
                        if (
                            total_balance is not None
                        ):  # we got this address balance from the db in a prior loop and it exists in the src20_valid_dict so we can use it
                            balances.append(BalanceCurrent(tick, address, D(total_balance), locked_balance))
                            addresses.remove(address)
        except Exception as e:
            logger.error(f"An exception in user balance: {e}")
            raise

    if addresses:
        try:
            total_balance_tuple = get_total_user_balance_from_balances_db(db, tick, tick_hash, addresses)
            for address in addresses:
                total_balance = next(
                    (balance.total_balance for balance in total_balance_tuple if balance.address == address),
                    0,
                )
                locked_balance = next(
                    (balance.locked_amt for balance in total_balance_tuple if balance.address == address),
                    0,
                )  # NOTE: this is not fully implemented
                # if total_balance is negative throw an exception
                if total_balance < 0:
                    raise Exception(f"Negative balance for address {address} in tick {tick}")
                balances.append(
                    BalanceCurrent(
                        tick,
                        address,
                        D(total_balance),
                        locked_balance if total_balance != 0 else 0,
                    )
                )
        except Exception as e:
            print(f"An exception occurred: {e}")
            raise

    return balances


def get_total_user_balance_from_balances_db(db, tick, tick_hash, addresses):
    """a revised version of get_total_user_balance_from_db to fetch only from
    the balances table, this should be much more efficient, and we can do
    a cross check against the get_total_user_balance_from_db to validate and
    for balance table rebuilds"""

    if isinstance(addresses, str):
        addresses = [addresses]

    balances = []
    BalanceTuple = namedtuple(
        "BalanceTuple",
        [
            "tick",
            "address",
            "total_balance",
            "highest_block_index",
            "block_time_unix",
            "locked_amt",
        ],
    )

    with db.cursor() as src20_cursor:
        query = f"""
            SELECT
                tick,
                address,
                amt,
                last_update,
                block_time,
                locked_amt
            FROM
                {SRC20_BALANCES_TABLE}
            WHERE
                tick = %s
                AND tick_hash = %s
                AND address IN %s
        """  # nosec

        src20_cursor.execute(query, (tick, tick_hash, tuple(addresses)))
        results = src20_cursor.fetchall()
        for address in addresses:
            total_balance = D("0")
            highest_block_index = 0
            block_time_unix = None
            for result in results:
                tick = result[0]
                address = result[1]
                total_balance = result[2]
                highest_block_index = result[3]
                block_time_unix = result[4]
                locked_amt = result[5]
                balances.append(
                    BalanceTuple(
                        tick,
                        address,
                        total_balance,
                        highest_block_index,
                        block_time_unix,
                        locked_amt,
                    )
                )

    return balances


def get_total_user_balance_from_db(db, tick, tick_hash, addresses):
    """another heavy operation to be running on every creator/tick pair
    this is for validation, the speedy version should pull from the balances table
    keep in mind balance table is not committed on each transaction
    The address list must be unique addresses"""

    if isinstance(addresses, str):
        addresses = [addresses]

    balances = []
    BalanceTuple = namedtuple(
        "BalanceTuple",
        ["tick", "address", "total_balance", "highest_block_index", "block_time_unix"],
    )

    with db.cursor() as src20_cursor:
        query = f"""
            SELECT
                amt,
                op,
                destination,
                creator,
                block_index,
                UNIX_TIMESTAMP(block_time) AS block_time_unix
            FROM
                {SRC20_VALID_TABLE}
            WHERE
                tick = %s
                AND tick_hash = %s
                AND (destination IN %s OR creator IN %s)
                AND (op = 'TRANSFER' OR op = 'MINT')
            ORDER BY block_index
        """  # nosec

        src20_cursor.execute(query, (tick, tick_hash, tuple(addresses), tuple(addresses)))
        # src20_cursor.execute(query, {'tick': tick, 'tick_hash': tick_hash, 'addresses': tuple(addresses)})
        results = src20_cursor.fetchall()
        for address in addresses:
            total_balance = D("0")
            highest_block_index = 0
            q_block_time_unix = None
            for result in results:
                q_amt = D(result[0])
                q_op = result[1]
                q_destination = result[2]
                q_creator = result[3]
                q_block_index = result[4]
                q_block_time_unix = result[5]
                if q_block_index > highest_block_index:
                    highest_block_index = q_block_index
                if q_op == "MINT" and q_destination == address:
                    total_balance += q_amt
                if q_op == "TRANSFER" and q_destination == address:
                    total_balance += q_amt
                if q_op == "TRANSFER" and q_creator == address:
                    total_balance -= q_amt
            balances.append(BalanceTuple(tick, address, total_balance, highest_block_index, q_block_time_unix))

    return balances


def get_tick_holders_from_balances(db, tick):
    """
    Retrieve the addresses of all tick holders with a balance greater than zero in the prior block.
    This function is not aware of pending / uncommitted transactions.

    Parameters:
    - db: The database connection object.
    - tick: The tick value.

    Returns:
    - tick_holders: A list of addresses of tick holders with a balance greater than zero.
    """
    tick_holders = []
    with db.cursor() as src20_cursor:
        src20_cursor.execute(
            f"""
            SELECT
                address
            FROM
                {SRC20_BALANCES_TABLE}
            WHERE
                tick = %s
                AND amt > 0
        """,
            (tick,),
        )  # nosec
        for row in src20_cursor.fetchall():
            tick_holders.append(row[0])
    return tick_holders


def update_src20_balances(db, block_index, block_time, processed_src20_in_block):
    balance_updates: List[Dict[str, Union[str, D]]] = []

    for src20_dict in processed_src20_in_block:
        if src20_dict.get("valid") == 1:

            try:
                if src20_dict["op"] == "MINT":
                    # Credit to destination (creator can be a mint service)
                    balance_dict = next(
                        (
                            item
                            for item in balance_updates
                            if item["tick"] == src20_dict["tick"]
                            and item["tick_hash"] == src20_dict["tick_hash"]
                            and item["address"] == src20_dict["destination"]
                        ),
                        None,
                    )
                    if balance_dict is None:
                        balance_dict = {
                            "tick": src20_dict["tick"],
                            "tick_hash": src20_dict["tick_hash"],
                            "address": src20_dict["destination"],
                            "credit": D(src20_dict["amt"]),
                            "debit": D(0),
                        }
                        balance_updates.append(balance_dict)
                    else:
                        balance_dict["credit"] += D(src20_dict["amt"])

                elif src20_dict["op"] == "TRANSFER":
                    # Debit from creator
                    balance_dict = next(
                        (
                            item
                            for item in balance_updates
                            if item["tick"] == src20_dict["tick"]
                            and item["tick_hash"] == src20_dict["tick_hash"]
                            and item["address"] == src20_dict["creator"]
                        ),
                        None,
                    )
                    if balance_dict is None:
                        balance_dict = {
                            "tick": src20_dict["tick"],
                            "tick_hash": src20_dict["tick_hash"],
                            "address": src20_dict["creator"],
                            "credit": D(0),
                            "debit": D(src20_dict["amt"]),
                        }
                        balance_updates.append(balance_dict)
                    else:
                        balance_dict["debit"] += D(src20_dict["amt"])

                    # Credit to destination
                    balance_dict = next(
                        (
                            item
                            for item in balance_updates
                            if item["tick"] == src20_dict["tick"]
                            and item["tick_hash"] == src20_dict["tick_hash"]
                            and item["address"] == src20_dict["destination"]
                        ),
                        None,
                    )
                    if balance_dict is None:
                        balance_dict = {
                            "tick": src20_dict["tick"],
                            "tick_hash": src20_dict["tick_hash"],
                            "address": src20_dict["destination"],
                            "credit": D(src20_dict["amt"]),
                            "debit": D(0),
                        }
                        balance_updates.append(balance_dict)
                    else:
                        balance_dict["credit"] += D(src20_dict["amt"])

            except Exception as e:
                logger.error(f"Error updating SRC20 balances: {e}")
                raise e

    if balance_updates:
        update_balance_table(db, balance_updates, block_index, block_time)
    return balance_updates


def update_balance_table(db, balance_updates, block_index, block_time):
    """Update the balances table with the balance_updates list"""
    cursor = db.cursor()

    for balance_dict in balance_updates:
        try:
            net_change = balance_dict.get("credit", D(0)) - balance_dict.get("debit", D(0))
            balance_dict["net_change"] = net_change
            id_field = balance_dict["tick"] + "_" + balance_dict["address"]

            cursor.execute(f"SELECT amt FROM {SRC20_BALANCES_TABLE} WHERE id = %s", (id_field,))  # nosec
            result = cursor.fetchone()
            if result is not None:
                balance_dict["original_amt"] = D(result[0])
            else:
                balance_dict["original_amt"] = D(0)

            cursor.execute(
                """
                INSERT INTO balances
                (id, address, tick, amt, last_update, block_time, p, tick_hash)
                VALUES (%s, %s, %s, %s, %s, FROM_UNIXTIME(%s), %s, %s)
                ON DUPLICATE KEY UPDATE
                    amt = amt + VALUES(amt),
                    last_update = VALUES(last_update)
            """,
                (
                    id_field,
                    balance_dict["address"],
                    balance_dict["tick"],
                    net_change,
                    block_index,
                    block_time,
                    "SRC-20",
                    balance_dict["tick_hash"],
                ),
            )

        except Exception as e:
            logger.error("Error updating balances table:", e)
            raise e

    cursor.close()
    return


def process_balance_updates(balance_updates):
    """
    Process the balance updates and return a string representation of valid src20 entries.

    Args:
        balance_updates (list): A list of balance updates.

    Returns:
        str: A string representation of valid src20 entries.
    """

    valid_src20_list = []
    if balance_updates:
        for src20 in balance_updates:
            creator = src20.get("address")
            tick = src20.get("tick", "")
            if "\\" in tick:
                tick = decode_unicode_escapes(tick)

            amt = D(src20.get("net_change", D(0))) + D(src20.get("original_amt", D(0)))
            amt = amt.normalize()

            amt_str = format_decimal(amt)
            valid_src20_list.append(f"{tick},{creator},{amt_str}")

    valid_src20_list = sorted(
        valid_src20_list,
        key=lambda src20: (src20.split(",")[0] + "_" + src20.split(",")[1]),
    )

    valid_src20_str = ";".join(valid_src20_list)
    return valid_src20_str


def format_decimal(amt):
    amt = amt.normalize()
    if amt == int(amt):
        return f"{int(amt)}"
    else:
        s = format(amt, "f").rstrip("0").rstrip(".")
        if not s:
            s = "0"
        return s


def clear_zero_balances(db):
    """
    Deletes all balances with an amount of 0 from the database.

    Args:
        db: The database connection object.

    Returns:
        None
    """
    with db.cursor() as cursor:
        cursor.execute(f"DELETE FROM {SRC20_BALANCES_TABLE} WHERE amt = 0")  # nosec
    return


def fetch_api_ledger_data(block_index: int):
    urls = []
    # if SRC_VALIDATION_API1:
    #     urls.append(SRC_VALIDATION_API1 + str(block_index))  # OKX diverges on hashes at 856444 due to their sci notation in strings
    if SRC_VALIDATION_SECRET_API2 and SRC_VALIDATION_API2:
        urls.append(SRC_VALIDATION_API2.format(block_index=block_index, secret=SRC_VALIDATION_SECRET_API2))

    if not urls:
        return None, None

    max_retries = 5
    backoff_time = 1

    def fetch_url(url):
        try:
            response = requests.get(url, timeout=5)
            logger.debug(f"Fetching URL: {url}")
            logger.debug(f"Response status code: {response.status_code}")
            logger.debug(f"Response headers: {response.headers}")
            logger.debug(f"Raw response text: {response.text}")

            if response.status_code == 200:
                try:
                    data = response.json()
                    logger.debug(f"Parsed JSON data: {data}")

                    if "data" in data:
                        data = data["data"]
                        logger.debug(f"Data field: {data}")

                        api_ledger_hash = data.get("hash")
                        api_ledger_validation = data.get("balance_data")

                        logger.debug(f"api_ledger_hash: {api_ledger_hash}")
                        logger.debug(f"api_ledger_validation: {api_ledger_validation}")

                        if not api_ledger_validation:
                            logger.error("api_ledger_validation is empty")
                            return None, None

                        return api_ledger_hash, api_ledger_validation
                    else:
                        logger.error("No 'data' key in response JSON")
                        return None, None
                except JSONDecodeError as e:
                    logger.error(f"JSONDecodeError: {e}")
                    logger.debug(f"Response content: {response.content}")
                    return None, None
                except Exception as e:
                    logger.error(f"Unexpected error parsing JSON: {e}")
                    return None, None
            else:
                logger.error(f"Non-200 response code: {response.status_code} from URL: {url}")
                return None, None
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for URL {url}: {e}")
            return None, None

    for _ in range(max_retries):
        with ThreadPoolExecutor() as executor:
            future_to_url = {executor.submit(fetch_url, url): url for url in urls}
            for future in as_completed(future_to_url):
                result = future.result()
                if result != (None, None):
                    return result

        time.sleep(backoff_time)
        backoff_time *= 2

    raise Exception(f"Failed to retrieve from the API after {max_retries} retries")


def validate_src20_ledger_hash(block_index: int, ledger_hash: str, valid_src20_str: str):
    try:
        api_ledger_hash, api_ledger_validation = fetch_api_ledger_data(block_index)
        if api_ledger_validation is None:
            raise ValueError(f"API ledger validation data is None. Local ledger_hash: {ledger_hash}")
    except Exception as e:
        logger.error(f"Error fetching API data: {e}")
        # Continue processing even if API data is unavailable
        return False

    if api_ledger_hash == ledger_hash:
        return True

    logger.warning("API ledger hash does not match local ledger hash")
    logger.warning("API Hash: %s", api_ledger_hash)
    logger.warning("Local Hash: %s", ledger_hash)

    local_balances = parse_balances(valid_src20_str)
    api_balances = parse_balances(api_ledger_validation)

    differences = compare_balances(local_balances, api_balances)

    if differences:
        print_balance_differences(differences)
    else:
        print("\nNo differences in balances found, despite hash mismatch.")

    compare_string_formats(valid_src20_str, api_ledger_validation)

    return True
    # If you want to raise an exception instead, you can uncomment the following line
    # raise ValueError("API ledger hash does not match local ledger hash")


def parse_balances(balance_str):
    balances = defaultdict(lambda: defaultdict(D))
    for entry in balance_str.split(";"):
        tick, address, balance = entry.split(",")
        balances[tick][address] = D(balance)
    return balances


def compare_balances(local_balances, api_balances):
    differences = []
    all_addresses = set()
    for tick_balances in local_balances.values():
        all_addresses.update(tick_balances.keys())
    for tick_balances in api_balances.values():
        all_addresses.update(tick_balances.keys())

    for address in sorted(all_addresses):
        address_differences = []
        for tick in sorted(set(local_balances.keys()) | set(api_balances.keys())):
            local_balance = local_balances.get(tick, {}).get(address, D("0"))
            api_balance = api_balances.get(tick, {}).get(address, D("0"))
            if local_balance != api_balance:
                address_differences.append((tick, local_balance, api_balance))
        if address_differences:
            differences.append((address, address_differences))

    return differences


def print_balance_differences(differences):
    print("\nBalance Differences:")
    print("--------------------")
    for address, address_differences in differences:
        print(f"\nAddress: {address}")
        print("  {:<10} {:<20} {:<20} {:<20}".format("Tick", "Local Balance", "API Balance", "Difference"))
        print("  " + "-" * 70)
        for tick, local_balance, api_balance in address_differences:
            difference = local_balance - api_balance
            print(
                "  {:<10} {:<20} {:<20} {:<20}".format(tick, f"{local_balance:.8f}", f"{api_balance:.8f}", f"{difference:.8f}")
            )


def compare_string_formats(local_str: str, api_str: str):
    print("\nComparing string formats:")
    print("-------------------------")

    local_entries = set(local_str.split(";"))
    api_entries = set(api_str.split(";"))

    local_only = local_entries - api_entries
    api_only = api_entries - local_entries

    if local_only:
        print("\nEntries only in local string:")
        for entry in sorted(local_only):
            print(f"  {entry}")

    if api_only:
        print("\nEntries only in API string:")
        for entry in sorted(api_only):
            print(f"  {entry}")

    if not local_only and not api_only:
        print("\nAll entries are present in both strings.")

    # Check for sorting differences
    local_sorted = sorted(local_entries)
    api_sorted = sorted(api_entries)

    if local_sorted != api_sorted:
        print("\nSorting difference detected:")
        print("  Local sorted:", ";".join(local_sorted))
        print("  API sorted:  ", ";".join(api_sorted))
        sys.exit()
    else:
        print("\nBoth strings have the same sorting order.")

    local_formatted = set(normalize_entry(entry) for entry in local_entries)
    api_formatted = set(normalize_entry(entry) for entry in api_entries)

    format_diff = local_formatted.symmetric_difference(api_formatted)
    if format_diff:
        print("\nFormatting differences detected:")
        for entry in sorted(format_diff):
            local_entry = next((e for e in local_entries if normalize_entry(e) == entry), None)
            api_entry = next((e for e in api_entries if normalize_entry(e) == entry), None)
            print(f"  Local: {local_entry}")
            print(f"  API:   {api_entry}")
            print()
    else:
        print("\nNo formatting differences detected.")


def normalize_entry(entry):
    token, address, amount = entry.split(",")
    normalized_amount = format(D(amount), ".18f").rstrip("0").rstrip(".")
    return f"{token},{address},{normalized_amount}"
