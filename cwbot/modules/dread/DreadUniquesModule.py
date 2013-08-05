from cwbot.modules.BaseDungeonModule import BaseDungeonModule, eventDbMatch
from cwbot.common.exceptions import FatalError
from cwbot.util.textProcessing import stringToBool
from collections import defaultdict, Counter


class DreadUniquesModule(BaseDungeonModule):
    """ 
    Displays which per-instance items are still in Dreadsylvania
    
    Configuration options:
    
    announce - if new items should be announced (True)
    """
    requiredCapabilities = ['chat', 'dread']
    _name = "dread-uniques"
    
    _areaNames = ["The Woods", "The Village", "The Castle"]
    
    def __init__(self, manager, identity, config):
        self._announce = None
        self._db = None
        self._dread = None
        self._uniques = None
        self._claimed = None
        super(DreadUniquesModule, self).__init__(manager, identity, config)

        
    def initialize(self, state, initData):
        self._claimed = None
        self._db = initData['event-db']
        self._uniques = [r for r in self._db if r['unique_text']]
        self._processLog(initData)


    def _configure(self, config):
        self._announce = stringToBool(config.setdefault('announce', "True"))


    @property
    def state(self):
        return {}

    
    @property
    def initialState(self):
        return {}

    
    def _processLog(self, raidlog):
        events = raidlog['events']
        
        # get dread status
        try:
            replies = self._raiseEvent("dread", "dread-overview", 
                                       data={'style': 'dict',
                                             'keys': ['status',
                                                      'index',
                                                      'locked']})
            self._dread = replies[0].data
        except IndexError:
            raise FatalError("DreadUniquesModule requires a "
                             "DreadOverviewModule with higher priority")

        newClaimed = defaultdict(list)
        userNames = {}
        for record in self._uniques:
            itemTxt = record['unique_text']
            quantity = 1
            try:
                quantity = int(record['quantity'])
            except ValueError:
                pass
            
            # see how many items were taken and who did it
            itemsAcquired = 0
            logMessage = ""
            for e in eventDbMatch(events, record):
                logMessage = e['event']
                newClaimed[itemTxt].extend([e['userId']] * e['turns'])
                userNames[e['userId']] = e['userName']
                itemsAcquired += e['turns']
            
            # compare to old list and announce if specified in the options
            if self._claimed is not None:
                # count up which users got this log message
                c1 = Counter(self._claimed.get(itemTxt, []))
                c2 = Counter(newClaimed.get(itemTxt, []))
                for k,v in c2.items():
                    # iterate through each user
                    numCollected = v - c1[k]
                    # print a pickup message for each time user X got item
                    # should be only once for dreadsylvania
                    for _ in range(numCollected):
                        self.chat("{} {} ({} remaining)."
                                  .format(userNames[k], 
                                          logMessage,
                                          quantity - itemsAcquired))
        self._claimed = newClaimed
        return True

            
    def _processDungeon(self, txt, raidlog):
        self._processLog(raidlog)
        return None
        
        
    def _processCommand(self, unused_msg, cmd, unused_args):
        if cmd in ["unique", "uniques"]:
            if not self._dungeonActive() or self._claimed is None:
                return ("Dreadsylvania has faded into the mist, along with "
                        "all its stuff. Don't you just hate when that "
                        "happens?")
            messages = defaultdict(list)
            qtyText = lambda x: "{}x ".format(x)
            for record in self._uniques:
                quantity = 1
                try:
                    quantity = int(record['quantity'])
                except ValueError:
                    pass

                itemData = self._dread[record['category']]
                if itemData['status'] in ["done", "boss"]:
                    continue

                itemName = record['unique_text']
                numAvailable = (quantity - len(self._claimed[itemName]))
                if numAvailable == 0:
                    continue
                if record['subzone'] in itemData['locked']:
                    messages[record['category']].append(
                            "{}LOCKED {}".format(qtyText(numAvailable),
                                                  itemName))
                else:
                    messages[record['category']].append(
                            "{}{}".format(qtyText(numAvailable), itemName))
            
            # get list of areas by index
            areas = dict((v['index'], k) for k,v in self._dread.items())
            txt = []
            for idx in areas.keys():
                areaname = areas[idx]
                if messages[areaname]:
                    txt.append("{}: {}."
                               .format(areaname, 
                                       ", ".join(messages[areaname])))
            if txt:
                return "\n".join(txt)
            return ("Looks like adventurers have combed over Dreadsylvania "
                    "pretty well.")
        return None
        
                
    def _availableCommands(self):
        return {'uniques': "!uniques: Show which Dreadsylvanian unique items "
                            "are still available."}    
        