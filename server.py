
import os
import decimal
import pprint
import sys
import pymysql as mysql
import time
import dateutil.parser
import calendar
import traceback
import binascii
import socket
import signal
import appdirs
import platform
import bitcoin as bitcoinlib
import logging
from urllib.parse import quote_plus as urlencode

import src.log as log
import config
import src.util as util
import src.exceptions as exceptions
import blocks
import check
import src.backend as backend
import src.database as database
import src.script as script

logger = logging.getLogger(__name__)
log.set_logger(logger)  # set root logger

D = decimal.Decimal


class ConfigurationError(Exception):
    pass

def sigterm_handler(_signo, _stack_frame):
    if _signo == 15:
        signal_name = 'SIGTERM'
    elif _signo == 2:
        signal_name = 'SIGINT'
    else:
        assert False
    logger.info('Received {}.'.format(signal_name))
    logger.info('Stopping backend.')
    # backend.stop() this would typically stop addrindexrs
    logger.info('Shutting down.')
    logging.shutdown()
    sys.exit(0)
signal.signal(signal.SIGTERM, sigterm_handler)
signal.signal(signal.SIGINT, sigterm_handler)


## MySQL Locking Function
# This code creates a table called server_lock in the MySQL database and inserts a single row into the table.
# If another instance of the server tries to insert a row into the table, it will fail with an IntegrityError, 
# indicating that another copy of the server is already running.

# class LockingError(Exception):
#     pass

# def get_lock():
#     logger.info('Acquiring lock.')

#     db = mysql.connector.connect(
#         host='your-mysql-hostname',
#         user='your-username',
#         password='your-password',
#         database='your-database-name'
#     )
#     cursor = db.cursor()

#     try:
#         cursor.execute('CREATE TABLE server_lock (id INT PRIMARY KEY)')
#     except mysql.connector.errors.ProgrammingError:
#         pass

#     try:
#         cursor.execute('INSERT INTO server_lock (id) VALUES (1)')
#         db.commit()
#     except mysql.connector.errors.IntegrityError:
#         raise LockingError('Another copy of server is currently running.')

#     logger.debug('Lock acquired.')

# Lock database access by opening a socket.
class LockingError(Exception):
    pass


def initialise(*args, **kwargs):
    initialise_config(*args, **kwargs)
    return initialise_db()


def initialise_config(
    log_file=None,
    api_log_file=None,
    testnet=False, testcoin=False, regtest=False,
    api_limit_rows=1000,
    backend_name=None, backend_connect=None, backend_port=None,
    backend_user=None, backend_password=None,
    indexd_connect=None, indexd_port=None,
    backend_ssl=False, backend_ssl_no_verify=False,
    backend_poll_interval=None,
    rpc_host=None, rpc_port=None,
    rpc_user=None, rpc_password=None,
    rpc_no_allow_cors=False,
    force=False, verbose=False, console_logfilter=None,
    requests_timeout=config.DEFAULT_REQUESTS_TIMEOUT,
    rpc_batch_size=config.DEFAULT_RPC_BATCH_SIZE,
    check_asset_conservation=config.DEFAULT_CHECK_ASSET_CONSERVATION,
    backend_ssl_verify=None, rpc_allow_cors=None, p2sh_dust_return_pubkey=None,
    utxo_locks_max_addresses=config.DEFAULT_UTXO_LOCKS_MAX_ADDRESSES,
    utxo_locks_max_age=config.DEFAULT_UTXO_LOCKS_MAX_AGE,
    estimate_fee_per_kb=None,
    customnet=None, checkdb=False
):

    # Data directory
    data_dir = appdirs.user_data_dir(appauthor=config.XCP_NAME, appname=config.APP_NAME, roaming=True)
    if not os.path.isdir(data_dir):
        os.makedirs(data_dir, mode=0o755)

    print("data_dir: {}".format(data_dir))
    print("log_file: {}".format(log_file))

    # testnet
    if testnet:
        config.TESTNET = testnet
    else:
        config.TESTNET = False

    # testcoin
    if testcoin:
        config.TESTCOIN = testcoin
    else:
        config.TESTCOIN = False

    # regtest
    if regtest:
        config.REGTEST = regtest
    else:
        config.REGTEST = False

    if customnet is not None and len(customnet) > 0:
        config.CUSTOMNET = True
        config.REGTEST = True # Custom nets are regtests with different parameters
    else:
        config.CUSTOMNET = False

    if config.TESTNET:
        bitcoinlib.SelectParams('testnet')
    elif config.REGTEST:
        bitcoinlib.SelectParams('regtest')
    else:
        bitcoinlib.SelectParams('mainnet')

    network = ''
    if config.TESTNET:
        network += '.testnet'
    if config.REGTEST:
        network += '.regtest'
    if config.TESTCOIN:
        network += '.testcoin'

    if checkdb:
        config.CHECKDB = True
    else:
        config.CHECKDB = False

    # Log directory
    log_dir = appdirs.user_log_dir(appauthor=config.XCP_NAME, appname=config.APP_NAME)
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir, mode=0o755)

    # Log
    if log_file is False:  # no file logging
        config.LOG = None
    elif not log_file:  # default location
        filename = 'server{}.log'.format(network)
        config.LOG = os.path.join(log_dir, filename)
    else:  # user-specified location
        config.LOG = log_file

    # Set up logging.
    log.set_up(log.ROOT_LOGGER, verbose=verbose, logfile=config.LOG, console_logfilter=console_logfilter)
    if config.LOG:
        logger.debug('Writing server log to file: `{}`'.format(config.LOG))

    # Log unhandled errors.
    def handle_exception(exc_type, exc_value, exc_traceback):
        logger.error("Unhandled Exception", exc_info=(exc_type, exc_value, exc_traceback))
    sys.excepthook = handle_exception

    config.API_LIMIT_ROWS = api_limit_rows

    ##############
    # THINGS WE CONNECT TO

    # Backend name
    config.BACKEND_NAME = 'bitcoincore'

    # Backend RPC host (Bitcoin Core)
    if backend_connect:
        config.BACKEND_CONNECT = backend_connect
    else:
        config.BACKEND_CONNECT = 'localhost'

    # Backend Core RPC port (Bitcoin Core)
    if backend_port:
        config.BACKEND_PORT = backend_port
    else:
        if config.TESTNET:
            config.BACKEND_PORT = config.DEFAULT_BACKEND_PORT_TESTNET
        elif config.REGTEST:
            config.BACKEND_PORT = config.DEFAULT_BACKEND_PORT_REGTEST
        else:
            config.BACKEND_PORT = config.DEFAULT_BACKEND_PORT

    try:
        config.BACKEND_PORT = int(config.BACKEND_PORT)
        if not (int(config.BACKEND_PORT) > 1 and int(config.BACKEND_PORT) < 65535):
            raise ConfigurationError('invalid backend API port number')
    except:
        raise ConfigurationError("Please specific a valid port number backend-port configuration parameter")

    # Backend Core RPC user (Bitcoin Core)
    if backend_user:
        config.BACKEND_USER = backend_user
    else:
        config.BACKEND_USER = 'bitcoinrpc'

    # Backend Core RPC password (Bitcoin Core)
    if backend_password:
        config.BACKEND_PASSWORD = backend_password
    else:
        raise ConfigurationError('backend RPC password not set. (Use configuration file or --backend-password=PASSWORD)')

    # Backend Core RPC SSL
    if backend_ssl:
        config.BACKEND_SSL = backend_ssl
    else:
        config.BACKEND_SSL = False  # Default to off.

    # Backend Core RPC SSL Verify
    if backend_ssl_verify is not None:
        logger.warning('The server parameter `backend_ssl_verify` is deprecated. Use `backend_ssl_no_verify` instead.')
        config.BACKEND_SSL_NO_VERIFY = not backend_ssl_verify
    else:
        if backend_ssl_no_verify:
            config.BACKEND_SSL_NO_VERIFY = backend_ssl_no_verify
        else:
            config.BACKEND_SSL_NO_VERIFY = False # Default to on (don't support self‐signed certificates)

    # Backend Poll Interval
    if backend_poll_interval:
        config.BACKEND_POLL_INTERVAL = backend_poll_interval
    else:
        config.BACKEND_POLL_INTERVAL = float(
            os.environ.get('BACKEND_POLL_INTERVAL', "0.5")
        )

    # Construct backend URL.
    config.BACKEND_URL = config.BACKEND_USER + ':' + config.BACKEND_PASSWORD + '@' + config.BACKEND_CONNECT + ':' + str(config.BACKEND_PORT)
    if config.BACKEND_SSL:
        config.BACKEND_URL = 'https://' + config.BACKEND_URL
    else:
        config.BACKEND_URL = 'http://' + config.BACKEND_URL


    ##############
    # OTHER SETTINGS

    # skip checks
    if force:
        config.FORCE = force
    else:
        config.FORCE = False

    # Encoding
    if config.TESTCOIN:
        config.PREFIX = b'XX'
        config.CP_PREFIX = b'CNTRPRTY'
    else:
        config.PREFIX = b'stamp:' 
        config.CP_PREFIX = b'CNTRPRTY'

    # (more) Testnet
    if config.TESTNET:
        config.MAGIC_BYTES = config.MAGIC_BYTES_TESTNET
        if config.TESTCOIN:
            config.ADDRESSVERSION = config.ADDRESSVERSION_TESTNET
            config.P2SH_ADDRESSVERSION = config.P2SH_ADDRESSVERSION_TESTNET
            config.BLOCK_FIRST = config.BLOCK_FIRST_TESTNET_TESTCOIN
            config.BURN_START = config.BURN_START_TESTNET_TESTCOIN
            config.BURN_END = config.BURN_END_TESTNET_TESTCOIN
            config.UNSPENDABLE = config.UNSPENDABLE_TESTNET
            config.P2SH_DUST_RETURN_PUBKEY = p2sh_dust_return_pubkey
        else:
            config.ADDRESSVERSION = config.ADDRESSVERSION_TESTNET
            config.P2SH_ADDRESSVERSION = config.P2SH_ADDRESSVERSION_TESTNET
            config.BLOCK_FIRST = config.BLOCK_FIRST_TESTNET
            config.BURN_START = config.BURN_START_TESTNET
            config.BURN_END = config.BURN_END_TESTNET
            config.UNSPENDABLE = config.UNSPENDABLE_TESTNET
            config.P2SH_DUST_RETURN_PUBKEY = p2sh_dust_return_pubkey
    elif config.CUSTOMNET:
        custom_args = customnet.split('|')

        if len(custom_args) == 3:
            config.MAGIC_BYTES = config.MAGIC_BYTES_REGTEST
            config.ADDRESSVERSION = binascii.unhexlify(custom_args[1])
            config.P2SH_ADDRESSVERSION = binascii.unhexlify(custom_args[2])
            config.BLOCK_FIRST = config.BLOCK_FIRST_REGTEST
            config.BURN_START = config.BURN_START_REGTEST
            config.BURN_END = config.BURN_END_REGTEST
            config.UNSPENDABLE = custom_args[0]
            config.P2SH_DUST_RETURN_PUBKEY = p2sh_dust_return_pubkey
        else:
            raise "Custom net parameter needs to be like UNSPENDABLE_ADDRESS|ADDRESSVERSION|P2SH_ADDRESSVERSION (version bytes in HH format)"
    elif config.REGTEST:
        config.MAGIC_BYTES = config.MAGIC_BYTES_REGTEST
        if config.TESTCOIN:
            config.ADDRESSVERSION = config.ADDRESSVERSION_REGTEST
            config.P2SH_ADDRESSVERSION = config.P2SH_ADDRESSVERSION_REGTEST
            config.BLOCK_FIRST = config.BLOCK_FIRST_REGTEST_TESTCOIN
            config.BURN_START = config.BURN_START_REGTEST_TESTCOIN
            config.BURN_END = config.BURN_END_REGTEST_TESTCOIN
            config.UNSPENDABLE = config.UNSPENDABLE_REGTEST
            config.P2SH_DUST_RETURN_PUBKEY = p2sh_dust_return_pubkey
        else:
            config.ADDRESSVERSION = config.ADDRESSVERSION_REGTEST
            config.P2SH_ADDRESSVERSION = config.P2SH_ADDRESSVERSION_REGTEST
            config.BLOCK_FIRST = config.BLOCK_FIRST_REGTEST
            config.BURN_START = config.BURN_START_REGTEST
            config.BURN_END = config.BURN_END_REGTEST
            config.UNSPENDABLE = config.UNSPENDABLE_REGTEST
            config.P2SH_DUST_RETURN_PUBKEY = p2sh_dust_return_pubkey
    else:
        config.MAGIC_BYTES = config.MAGIC_BYTES_MAINNET
        if config.TESTCOIN:
            config.ADDRESSVERSION = config.ADDRESSVERSION_MAINNET
            config.P2SH_ADDRESSVERSION = config.P2SH_ADDRESSVERSION_MAINNET
            config.BLOCK_FIRST = config.BLOCK_FIRST_MAINNET_TESTCOIN
            config.BURN_START = config.BURN_START_MAINNET_TESTCOIN
            config.BURN_END = config.BURN_END_MAINNET_TESTCOIN
            config.UNSPENDABLE = config.UNSPENDABLE_MAINNET
            config.P2SH_DUST_RETURN_PUBKEY = p2sh_dust_return_pubkey
        else:
            config.ADDRESSVERSION = config.ADDRESSVERSION_MAINNET
            config.P2SH_ADDRESSVERSION = config.P2SH_ADDRESSVERSION_MAINNET
            config.BLOCK_FIRST = config.BLOCK_FIRST_MAINNET
            config.BURN_START = config.BURN_START_MAINNET
            config.BURN_END = config.BURN_END_MAINNET
            config.UNSPENDABLE = config.UNSPENDABLE_MAINNET
            config.P2SH_DUST_RETURN_PUBKEY = p2sh_dust_return_pubkey

    # Misc
    config.REQUESTS_TIMEOUT = requests_timeout
    config.CHECK_ASSET_CONSERVATION = check_asset_conservation
    config.UTXO_LOCKS_MAX_ADDRESSES = utxo_locks_max_addresses
    config.UTXO_LOCKS_MAX_AGE = utxo_locks_max_age

    if estimate_fee_per_kb is not None:
        config.ESTIMATE_FEE_PER_KB = estimate_fee_per_kb

    # logger.info('Running v{} of counterparty-lib.'.format(config.VERSION_STRING))


def initialise_db():
    print("initialise_db")
    if config.FORCE:
        logger.warning('THE OPTION `--force` IS NOT FOR USE ON PRODUCTION SYSTEMS.')

    rds_host = os.environ.get('RDS_HOSTNAME')
    rds_user = os.environ.get('RDS_USER')
    rds_password = os.environ.get('RDS_PASSWORD')
    rds_database = os.environ.get('RDS_DATABASE')

    # Database
    logger.info('Connecting to database (MySQL).')
    db = mysql.connect(
        host=rds_host,
        user=rds_user,
        password=rds_password,
        port=3306,
        database=rds_database
    )
    util.CURRENT_BLOCK_INDEX = blocks.last_db_index(db)

    return db


def connect_to_backend():
    if not config.FORCE:
        logger.info('Connecting to BTC Node.')
        backend.getblockcount()


def start_all(db):

    # Backend.
    connect_to_backend()

    # Server.
    blocks.follow(db)


def reparse(db, block_index=None, quiet=True):
    connect_to_backend()
    blocks.reparse(db, block_index=block_index, quiet=quiet)


def kickstart(db, bitcoind_dir):
    blocks.kickstart(db, bitcoind_dir=bitcoind_dir)


def debug_config():
    output = vars(config)
    for k in list(output.keys()):
        if k[:2] == "__" and k[-2:] == "__":
            del output[k]

    pprint.pprint(output)


def configure_rpc(rpc_password=None):
    # Server API RPC password
    if rpc_password:
        config.RPC_PASSWORD = rpc_password
        config.RPC = 'http://' + urlencode(config.RPC_USER) + ':' + urlencode(config.RPC_PASSWORD) + '@' + config.RPC_HOST + ':' + str(config.RPC_PORT) + config.RPC_WEBROOT
    else:
        config.RPC = 'http://' + config.RPC_HOST + ':' + str(config.RPC_PORT) + config.RPC_WEBROOT


# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4








