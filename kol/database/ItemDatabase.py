############# MODIFIED FROM pyKol DISTRIBUTION ##############################

"This module is used as a database for KoL item information."

import kol.Error as Error
from kol.data import Items
from kol.manager import FilterManager
from kol.util import Report
from cwbot.kolextra.request.ItemDescriptionRequest import ItemDescriptionRequest
import cPickle as pickle
import os
import datetime
import copy

__isInitialized = False
__itemsById = {}
__itemsByDescId = {}
__itemsByName = {}
__discoveryDate = None
__isLoaded = False
__dbChanged = False

discoveryFile = "data/itemDiscovery.dat"

def init():
    """
    Initializes the ItemDatabase. This method should be called before the
    database is ever accessed as it ensures that the database is populated
    with all of the data it needs.
    """
    global __dbChanged
    __dbChanged = False
    loadItemsFromFile()
    global __isInitialized
    if __isInitialized == True:
        return

    Report.trace("itemdatabase", "Initializing the item database.")
    returnCode = FilterManager.executeFiltersForEvent("preInitializeItemDatabase")
    if returnCode == FilterManager.FINISHED:
        Report.trace("itemdatabase", "Item database initialized.")
        __isInitialized = True
        return

    for item in Items.items:
        addItem(item)

    FilterManager.executeFiltersForEvent("postInitializeItemDatabase")
    __isInitialized = True
    Report.trace("itemdatabase", "Item database initialized.")

def addItem(item):
    "Adds an item to the database."
    if "plural" not in item:
        item["plural"] = item["name"] + "s"
    __itemsById[item["id"]] = item
    __itemsByDescId[item["descId"]] = item
    __itemsByName[item["name"]] = item

def getItemFromId(itemId, session=None):
    "Returns information about an item given its ID."
    global __dbChanged

    if not __isInitialized:
        init()

    try:
        return __itemsById[itemId].copy()
    except KeyError:
        try:
            if session is None:
                raise KeyError()
            from kol.request.ItemInformationRequest import ItemInformationRequest
            r = ItemInformationRequest(session, itemId)
            result = r.doRequest()
            item = result["item"]
            addItem(item)
            Report.trace("itemdatabase", "Discovered new item: %s" % item["name"])
            context = { "item" : item }
            FilterManager.executeFiltersForEvent("discoveredNewItem", context, session=session, item=item)
            __dbChanged = True
            return item
        except (KeyError, Error.Error):
            raise Error.Error("Item ID %s is unknown." % itemId, Error.ITEM_NOT_FOUND)

def getOrDiscoverItemFromId(itemId, session):
    return _try3(getItemFromId, session, itemId, session)

def getItemFromDescId(descId, session=None):
    "Returns information about an item given its description ID."
    if not __isInitialized:
        init()

    try:
        return __itemsByDescId[descId].copy()
    except KeyError:
        myError = Error.Error("Item with description ID %s is unknown." % descId, Error.ITEM_NOT_FOUND)
        try:
            if session is None:
                raise KeyError()
            r = ItemDescriptionRequest(session, descId)
            result = r.doRequest()
            if not result or result['id'] is None:
                raise myError
            id_ = int(result['id'])
            if id_ in __itemsById:
                return __itemsById[id_].copy()
            else:
                try:
                    return getItemFromId(id_, session)
                except Error.Error:
                    return result
        except (KeyError, Error.Error):
            raise myError

def getOrDiscoverItemFromDescId(descId, session):
    return _try3(getItemFromDescId, session, descId, session)

def getItemFromName(itemName):
    "Returns information about an item given its name."
    if not __isInitialized:
        init()

    try:
        return __itemsByName[itemName].copy()
    except KeyError:
        raise Error.Error("The item '%s' is unknown." % itemName, Error.ITEM_NOT_FOUND)

def getOrDiscoverItemFromName(itemName, session):
    return _try3(getItemFromName, session, itemName)

def discoverMissingItems(session):
    global __dbChanged
    from kol.request.InventoryRequest import InventoryRequest
    from kol.request.ItemInformationRequest import ItemInformationRequest
    invRequest = InventoryRequest(session)
    invRequest.ignoreItemDatabase = True
    invData = invRequest.doRequest()
    for item in invData["items"]:
        if item["id"] not in __itemsById:
            try:
                itemRequest = ItemInformationRequest(session, item["id"])
                itemData = itemRequest.doRequest()
                item = itemData["item"]
                addItem(item)
                Report.trace("itemdatabase", "Discovered new item: %s" % item["name"])
                context = { "item" : item }
                FilterManager.executeFiltersForEvent("discoveredNewItem", context, session=session, item=item)
                __dbChanged = True
            except:
                pass

def loadItemsFromFile():
    global __dbChanged
    try:
        f = open(discoveryFile, 'rb')
        global __itemsById, __itemsByDescId, __itemsByName, __discoveryDate
        __discoveryDate = pickle.load(f)
        if __discoveryDate is None or (datetime.datetime.now() - __discoveryDate) < datetime.timedelta(days=14):
            __itemsById = pickle.load(f)
            __itemsByDescId = pickle.load(f)
            __itemsByName = pickle.load(f)
        else:
            Report.trace("itemdatabase", "Item cache expired")
            __discoveryDate = datetime.datetime.now()
            __dbChanged = True
            __itemsById = {}
            __itemsByDescId = {}
            __itemsByName = {}
            saveItemsToFile()
        f.close()
        Report.trace("itemdatabase", "Loaded %d items from file." % len(__itemsById))
    except:
        Report.trace("itemdatabase", "Error opening %s for loading" % (discoveryFile))
        __discoveryDate = datetime.datetime.now()
        __dbChanged = True
        __itemsById = {}
        __itemsByDescId = {}
        __itemsByName = {}
        deleteItemCache()
    
def saveItemsToFile():
    global __dbChanged
    if not __dbChanged:
        return
    try:
        f = open(discoveryFile, 'wb')
        pickle.dump(__discoveryDate, f)
        pickle.dump(__itemsById, f)
        pickle.dump(__itemsByDescId, f)
        pickle.dump(__itemsByName, f)
        f.close()
        Report.trace("itemdatabase", "Wrote %d items to file." % len(__itemsById))
        __dbChanged = False
    except:
        Report.trace("itemdatabase", "Error opening %s for writing" % (discoveryFile))
        deleteItemCache()
            
def deleteItemCache():
    try:
        Report.trace("itemdatabase", "Deleted item cache" % (discoveryFile))
        os.remove(discoveryFile)
    except:
        pass
        
def reset():
    saveItemsToFile()
    __isInitialized = False
    __itemsById = {}
    __itemsByDescId = {}
    __itemsByName = {}
    __discoveryDate = None
    __isLoaded = False
    init()

def _try3(func, session, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Error.Error:
        discoverMissingItems(session)
        try:
            return func(*args, **kwargs)
        except Error.Error:
            reset()
            discoverMissingItems(session)
            return func(*args, **kwargs)
