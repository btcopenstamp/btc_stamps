import logging
logger = logging.getLogger(__name__)
import time
import collections
import copy
import os
import pymysql as mysql
import config
import src.util as util
import src.exceptions as exceptions
import src.log as log

BLOCK_MESSAGES = []

def rowtracer(cursor, sql):
    """Converts fetched SQL data into dict-style"""
    dictionary = {}
    for index, (name, type_) in enumerate(cursor.getdescription()):
        dictionary[name] = sql[index]
    return dictionary

def exectracer(cursor, sql, bindings):
    # This means that all changes to database must use a very simple syntax.
    # TODO: Need sanity checks here.
    sql = sql.lower()

    if sql.startswith('create trigger') or sql.startswith('drop trigger'):
        #CREATE TRIGGER stmts may include an "insert" or "update" as part of them
        return True

    # Parse SQL.
    array = sql.split('(')[0].split(' ')
    command = array[0]
    if 'insert' in sql:
        category = array[2]
    elif 'update' in sql:
        category = array[1]
    else:
        #CREATE TABLE, etc
        return True

    db = cursor.getconnection()
    dictionary = {'command': command, 'category': category, 'bindings': bindings}
    print(dictionary)  # DEBUG

    skip_tables = [
        'blocks', 'transactions',
        'balances', 'messages', 'mempool', 'assets',
        'new_sends', 'new_issuances' # interim table for CIP10 activation
    ]
    skip_tables_block_messages = copy.copy(skip_tables)
    if command == 'update':
        # List message manually.
        skip_tables += ['orders', 'bets', 'rps', 'order_matches', 'bet_matches', 'rps_matches']

    # Record alteration in database.
    if category not in skip_tables:
        print(db, bindings, command, category, bindings)
        print('command') # DEBUG
        print('category', category) # DEBUG
        print('bindings', bindings) # DEBUG
        log.message(db, bindings[0], command, category, bindings)
    # Record alteration in computation of message feed hash for the block
    if category not in skip_tables_block_messages:
        # don't include asset_longname as part of the messages hash
        #   until subassets are enabled
        if category == 'issuances' and not util.enabled('subassets'):
            if isinstance(bindings, dict) and 'asset_longname' in bindings: del bindings['asset_longname']

        # don't include memo as part of the messages hash
        #   until enhanced_sends are enabled
        if category == 'sends' and not util.enabled('enhanced_sends'):
            if isinstance(bindings, dict) and 'memo' in bindings: del bindings['memo']

        sorted_bindings = sorted(bindings.items()) if isinstance(bindings, dict) else [bindings,]
        BLOCK_MESSAGES.append('{}{}{}'.format(command, category, sorted_bindings))

    return True


# MySQL Version of get_connection

class DatabaseIntegrityError(exceptions.DatabaseError):
     pass
def get_connection(read_only=True, foreign_keys=True, integrity_check=True):
    """Connects to the MySQL database, returning a db `Connection` object"""
    logger.debug('Creating connection to `{}`.'.format(config.DATABASE))
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

    cursor = db.cursor()

    # For integrity, security.
    if foreign_keys and not read_only:
        logger.info('Checking database foreign keys...')
        cursor.execute('''SET FOREIGN_KEY_CHECKS=1''')
        rows = list(cursor.execute('''SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_TYPE = 'FOREIGN KEY' AND CONSTRAINT_SCHEMA = DATABASE()'''))
        if rows and rows[0][0] > 0:
            raise exceptions.DatabaseError('Foreign key check failed.')
        logger.info('Foreign key check completed.')
    if integrity_check:
        logger.info('Checking database integrity...')
        cursor.execute('''CHECK TABLES''')
        rows = cursor.fetchall()
        if rows and rows[0][3] != 'OK':
            raise exceptions.DatabaseError('Integrity check failed.')
        logger.info('Integrity check completed.')
    db.setrowtrace(rowtracer)
    # db.setexectrace(exectracer)
    cursor.close()
    return db

def version(db):
    cursor = db.cursor()
    user_version = cursor.execute('PRAGMA user_version').fetchall()[0]['user_version']
    # manage old user_version
    if user_version == config.VERSION_MINOR:
        version_minor = user_version
        version_major = config.VERSION_MAJOR
        user_version = (config.VERSION_MAJOR * 1000) + version_minor
        cursor.execute('PRAGMA user_version = {}'.format(user_version))
    else:
        version_minor = user_version % 1000
        version_major = user_version // 1000
    return version_major, version_minor

def update_version(db):
    cursor = db.cursor()
    user_version = (config.VERSION_MAJOR * 1000) + config.VERSION_MINOR
    cursor.execute('PRAGMA user_version = {}'.format(user_version)) # Syntax?!
    logger.info('Database version number updated.')
