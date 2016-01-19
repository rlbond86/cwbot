import sqlite3 as sql
import json


def _closeConnection(con):
    if con:
        con.close()


def encode(obj):
    return json.dumps(obj, separators=(',', ':'), sort_keys=True)


def decode(jobj):
    return json.loads(jobj)


def _ver2(filename):
    con = None
    try:
        con = sql.connect(filename, timeout=10, 
                          isolation_level="EXCLUSIVE")
        with con:
            c = con.cursor()
            c.execute("PRAGMA user_version")
            ver = c.fetchone()[0]
            if ver < 2:
                # check for existence of mail and ver2_update tables
                c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = c.fetchall()
                table_list = [item[0] for item in tables]
                if 'mail' in table_list and 'ver2_update' in table_list:
                    c.execute("DROP TABLE ver2_update")
                    con.commit()
                    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = c.fetchall()
                    table_list = [item[0] for item in tables]
                
                if 'mail' in table_list and 'ver2_update' not in table_list:
                    # add date/time field to mail table
                    c.execute("PRAGMA table_info(mail)")
                    columns = c.fetchall()
                    if not any(True for entry in columns if entry[1] == 'timestamp'):
                        c.execute("CREATE TABLE IF NOT EXISTS "
                                  "ver2_update(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                                  "kmailId INTEGER, state TEXT, userId INTEGER, "
                                  "data TEXT, itemsOnly INTEGER, error INTEGER, "
                                  "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
                        c.execute("INSERT INTO ver2_update (id, kmailId, state, userId, data, itemsOnly, error)"
                                  "    SELECT id, kmailId, state, userId, data, itemsOnly, error"
                                  "    FROM mail")
                        con.commit()
                        c.execute("DROP TABLE mail")
                        c.execute("ALTER TABLE ver2_update RENAME TO mail")
                        con.commit()
                        
                if 'mail' not in table_list and 'ver2_update' in table_list:
                    c.execute("ALTER TABLE ver2_update RENAME TO mail")
                    con.commit()

                c.execute("PRAGMA user_version=2")
                return 2                
            elif ver == 2:
                c.execute("UPDATE mail SET timestamp=datetime('now') WHERE timestamp is NULL")
                con.commit()
                return ver
            else:
                raise Exception("Invalid database version: {}".format(ver))
    finally:
        _closeConnection(con)


class Database(object):
    """ A class that handles internal database operations. """
    
    
    _names = {'mail': 'mail', 'state': 'state', 'inventory': 'inventory'}
    def __init__(self, filename, upgradeFunc=_ver2):
        self._filename = filename

        # integrity check
        con = None
        try:
            con = sql.connect(self._filename, timeout=10, 
                              isolation_level="EXCLUSIVE")
            c = con.cursor()
            c.execute("VACUUM")
            c.execute("PRAGMA integrity_check")
            result = c.fetchall()
            if result != [(u'ok',)]:
                raise Exception("Database corrupted. First error = {}"
                                .format(result[0][0]))
        finally:
            _closeConnection(con)
            
        # update to new version
        self.createMailTransactionTable()
        self.version = upgradeFunc(filename)
        
        
    def createStateTable(self):
        """ Creates a table that reflects the state of modules for a manager
        """
        tableName = self._names['state']
        con = None
        try:
            con = sql.connect(self._filename, timeout=10, 
                              isolation_level="IMMEDIATE")
            with con:
                c = con.cursor()
                c.execute("CREATE TABLE IF NOT EXISTS "
                          "{}(id INTEGER PRIMARY KEY, manager TEXT, "
                          "module TEXT, state TEXT)".format(tableName))
        finally:
            _closeConnection(con)
        return tableName
            
            
    def updateStateTable(self, managerName, stateDict, purge=False):
        """ Update the state database for a manager. stateDict must have
        the format {'moduleName1': module1StateDict, 
                    'moduleName2': module2StateDict}.
        the state dictionaries are converted to text using JSON, so state
        values may only be composed of: 
        list, dict, str, unicode, int, long, float, bool, None.
        """
        
        tableName = self._names['state']
        con = None
        items = []
        for modName,sDict in stateDict.items():
            items.append((encode(sDict), managerName, modName))
        try:
            con = sql.connect(self._filename, timeout=10, 
                              isolation_level="IMMEDIATE")
            with con:
                c = con.cursor()
                if purge:
                    c.execute("DELETE FROM {} WHERE manager=?"
                              .format(tableName), (managerName,))
                for item in items:
                    c.execute("UPDATE {} SET state=? "
                              "WHERE manager=? AND module=?"
                              .format(tableName), item)
                    if c.rowcount == 0:
                        c.execute("INSERT INTO {}(state, manager, module) "
                                  "VALUES(?,?,?)"
                                  .format(tableName), item)
        finally:
            _closeConnection(con)
    
    
    def loadStateTable(self, managerName):
        tableName = self._names['state']
        con = None
        try:
            stateDict = {}
            con = sql.connect(self._filename, timeout=10)
            c = con.cursor()
            c.execute("SELECT module, state FROM {} "
                      "WHERE manager=?"
                      .format(tableName), (managerName,))
            for modName,sDict in c.fetchall():
                stateDict[modName] = decode(sDict)
            return stateDict
        finally:
            _closeConnection(con)
            
            
    def createMailTransactionTable(self):
        tableName = self._names['mail']
        con = None
        try:
            con = sql.connect(self._filename, timeout=10, 
                              isolation_level="IMMEDIATE")
            with con:
                c = con.cursor()
                c.execute("CREATE TABLE IF NOT EXISTS "
                          "{}(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                          "kmailId INTEGER, state TEXT, userId INTEGER, "
                          "data TEXT, itemsOnly INTEGER, error INTEGER, "
                          "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
                          .format(tableName))
        finally:
            _closeConnection(con)
        return tableName
        
        
    def createInventoryReservationTable(self):
        tableName = self._names['inventory']
        con = None
        try:
            con = sql.connect(self._filename, timeout=10,
                              isolation_level="IMMEDIATE")
            with con:
                c = con.cursor()
                c.execute("CREATE TABLE IF NOT EXISTS "
                          "{}(id INTEGER PRIMARY KEY, iid INTEGER,"
                          "reserved INTEGER, reservedBy TEXT, "
                          "reserveInfo INTEGER)"
                          .format(tableName))
        finally:
            _closeConnection(con)
        return tableName
        

    def getDbConnection(self, **kwargs):
        """ 
        Get a connection to the DB. Be careful with this and be sure
        to call connection.close() when you are done!
        """
        
        con = sql.connect(self._filename, **kwargs)
        con.row_factory = sql.Row
        return con
