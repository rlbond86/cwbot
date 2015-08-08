import time
import calendar
import re
import socket
import operator
import pytz #@UnresolvedImport
from itertools import chain
import xmltodict
from cwbot.common.kmailContainer import Kmail
from unidecode import unidecode
from xml.parsers.expat import ExpatError
from urllib2 import HTTPError, URLError, urlopen
from collections import defaultdict, namedtuple, deque
from fuzzywuzzy import fuzz #@UnresolvedImport
from cwbot.modules.BaseChatModule import BaseChatModule
import kol.util.Report
from cwbot.kolextra.request.ClanLogPartialRequest import \
                            ClanLogPartialRequest, CLAN_LOG_FAX
import cwbot.util.DebugThreading as threading


tz = pytz.timezone('America/Phoenix')
utc = pytz.utc

_Faxbot = namedtuple('Faxbot', ['name', 'id', 'xml'])
_FaxMatch = namedtuple('FaxMatch', ['monstername', 'forced', 'message'])
_FaxState = namedtuple('FaxState', ['requestTime', 'requestId'])
_faxbotToList = lambda fb: [1, fb.name, fb.id, fb.xml]
_listToFaxbot = lambda ls: _Faxbot(*ls[1:])

def utcTime():
    """ Epoch time in UTC """
    return calendar.timegm(time.gmtime())


class FaxMonsterEntry(object):
    """ container class used to hold fax codes """
    def __init__(self, name, code, faxbot):
        self.name = name.strip()
        self.code = code.strip().lower()
        self.faxbot = faxbot
        self._otherNames = []
    
    @property
    def nameList(self):
        """ list of all possible names to reference this object, including
        the .name, .code, and all the ._otherNames """
        return [self.name.lower(), self.code] + self._otherNames
    
    def addAlias(self, name):
        """ an alias -- not the name or code, but another name for reference 
        """
        self._otherNames.append(name.strip().lower())

    def contains(self, name):
        return name.strip().lower() in self.nameList
    
    def __repr__(self):
        return "((FaxMonsterEntry: {}->{} on {}))".format(self.nameList[0], 
                                 ', '.join(self.nameList[1:]),
                                 self.faxbot)
        
    def toDict(self):
        return {'name': self.name, 
                'code': self.code, 
                'other': self._otherNames, 
                'faxbot': _faxbotToList(self.faxbot)}
        
    @classmethod
    def fromDict(cls, d):
        obj = cls(d['name'], d['code'], _listToFaxbot(d['faxbot']))
        obj._otherNames = d['other']
        return obj


class FaxModule2(BaseChatModule):
    """ 
    A module that handles faxing, including fax lookup for unknown monster
    codes, reporting on incoming faxes, and reporting what's in the fax
    machine.
    
    Configuration options:
    faxbot_timeout - time to wait until giving up on a fax request [def. = 90]
    url_timeout - time to try to load XML page before timing out [def. = 15]
    [[[[xml]]]]
        BOT_NUMBER = URL_TO_XML
    [[[[success]]]]
        BOTNAME = SUCCESS_PATTERN
    [[[[alias]]]]
        ALIASNAME = ALIAS (monster alias name)
        
    Configuration example:
    [[[[xml]]]]
        1 = http://hogsofdestiny.com/faxbot/faxbot.xml
        2 = http://faust.kolbots.com/faustbot.xml
        3 = https://sourceforge.net/p/easyfax/code/HEAD/tree/Easyfax.xml?format=raw
    [[[[success]]]]
        FaxBot = has copied
        faustbot = has been delivered
        Easyfax = fax is ready
    [[[[alias]]]]
        lobsterfrogman = lfm    # now you can type '!fax lfm' 
    """
        
    requiredCapabilities = ['chat']
    _name = "fax"
    
    __lock = threading.RLock()
    _faxWait = 60
    _xmlMins = 30
    _checkFrequency = 15
    
    _defaultXml = {'1': "http://hogsofdestiny.com/faxbot/faxbot.xml",
                   '2': "http://faust.kolbots.com/faustbot.xml",
                   '3': "https://sourceforge.net/p/easyfax/"
                              "code/HEAD/tree/Easyfax.xml?format=raw"}
    _defaultSuccess = {'FaxBot': "has copied",
                       'faustbot': "has been delivered",
                       'Easyfax': "fax is ready"}
    
    def __init__(self, manager, identity, config):
        self._abortTime = None
        self._timeout = None
        self._xmlAddresses = None
        self._finishInitialization = threading.Event()
        self._initialized = False
        self._monsters = defaultdict(list)
        self._requestQueue = deque()
        self._faxReply = None
        self._delayMode = threading.Event()
        self._delayStart = 0
        self._faxState = None
        self._faxCommands = []
        self._success = None
        self._lastXmlUpdate = 0
        self._lastFaxCheck = 0
        
        # last request the bot made to FaxBot
        self._lastRequest, self._lastRequestTime = None, None
        # last monster in the fax log
        self._lastFax, self._lastFaxTime = None, None
        # username of last faxer / last time bot got a message from faxbot 
        self._lastFaxUname, self._lastFaxBotTime = None, None
        self._lastFaxCheck = 0

        super(FaxModule2, self).__init__(manager, identity, config)
    
    
    def _configure(self, config):
        try:
            self._abortTime = int(config.setdefault('faxbot_timeout', 90))
            self._timeout = int(config.setdefault('url_timeout', 15))
            self._xmlAddresses = config.setdefault('xml', self._defaultXml)
            success = config.setdefault('success', self._defaultSuccess)
            self._success = {''.join(k.lower()): v
                             for k,v in success.items()}
        except ValueError:
            raise Exception("Fax Module config error: "
                            "faxbot_timeout, url_timeout must be integral")
        self._alias = config.setdefault('alias', {'lobsterfrogman': 'lfm'})


    def initialize(self, state, initData):
        oldMonsterList = state.get('monsterlist', {})
        self._monsters.update({k: [FaxMonsterEntry.fromDict(val) for val in v]
                               for k,v in oldMonsterList.items()})
        
        self._finishInitialization.set()


    @property
    def state(self):
        return {'monsterlist': {k: [val.toDict() for val in v]
                                for k,v in self._monsters.items()}}

    
    @property
    def initialState(self):
        return {'monsterlist': {}}
            
    
    def getFaxMatch(self, args):
        '''Look up the monster in the list using fuzzy matching. '''
        splitArgs = args.split()
        
        # did we force?
        if any(s for s in splitArgs if s.strip().lower() == "force"):
            # make a new monster
            return _FaxMatch(splitArgs[0], True, 'forcing')
        
        # make list of all possible names/codes/aliases
        nameList = {}
        for k,v in self._monsters.items():
            names = set(chain.from_iterable(val.nameList for val in v))
            nameList[k] = names
        
        simplify = (lambda x: x.replace("'", "")
                               .replace("_", " ")
                               .replace("-", " ").lower())
        sArgs = simplify(args)

        # first, check for exact code/name/alias matches
        matches = []
        for k,names in nameList.items():
            if any(True for name in names if sArgs == simplify(name)):
                matches.append(k)
        if len(matches) == 1:
            return _FaxMatch(matches[0], False, 'exact match')
        
        
        # next, check for "close" matches
        scoreDiff = 15
        scores = {}
        for k,names in nameList.items():
            score1 = max(fuzz.partial_token_set_ratio(simplify(name), sArgs)
                         for name in names) 
            scores[k] = score1
        maxScore = max(scores.values())
        fuzzyMatchKeys = set(k for k,v in scores.items()
                             if v >= maxScore - scoreDiff)

        # also check for args as a subset of string or code
        detokenize = lambda x: ''.join(re.split(r"'|_|-| ", x)).lower()
        dArgs = detokenize(args)
        
        subsetMatchKeys = set()
        for k, names in nameList.items():
            if any(True for name in names if dArgs in detokenize(name)):
                subsetMatchKeys.add(k)
        
        ls = len(subsetMatchKeys)
        lf = len(fuzzyMatchKeys)
        matchKeys = subsetMatchKeys | fuzzyMatchKeys
        lm = len(matchKeys)
        
        if ls == 0 and lf == 1:
            m = matchKeys.pop()
            return _FaxMatch(m, False, "fuzzy match")
        elif lm == 1:
            m = matchKeys.pop()
            return _FaxMatch(m, False, "subset match")
        elif lm > 1 and lm < 6:
            possibleMatchStr = ", ".join(
                    (self._monsters[k][0].name for k in matchKeys))
            return _FaxMatch(None, False, 
                             ("Did you mean one of: {}?"
                              .format(possibleMatchStr)))
        elif lm > 1:
            return _FaxMatch(None, False, 
                             ("Matched {} monster names/codes; "
                              "please be more specific. Send \"!fax list\" for"
                              " monster list.".format(ls + lf)))

        return _FaxMatch(None, False, "No known monster with name/code "
                                      "matching '{0}'. "
                                      "Use '!fax {0} force' to force, "
                                      "or send \"!fax list\" for a list."
                                      .format(args))
        

    def checkForNewFax(self, announceInChat=True):
        """ See if a new fax has arrived, and possibly announce it if it has. 
        """
        with self.__lock:
            self._lastFaxCheck = utcTime()
            replyStr = None
            lastFaxTime = self._lastFaxTime
            lastFaxUname = self._lastFaxUname
            lastFaxMonster = self._lastFax
            event = self.updateLastFax()
            if (self._lastFax is not None and 
                    (lastFaxTime != self._lastFaxTime or 
                     lastFaxUname != self._lastFaxUname or 
                     lastFaxMonster != self._lastFax)):
                self.log("Received new fax {}".format(event))
            return replyStr
        
        
    def updateLastFax(self):
        """ Update what's in the Fax machine """
        with self.__lock:
            
            # suppress annoying output from pyKol
            kol.util.Report.removeOutputSection("*")
            try:
                r = ClanLogPartialRequest(self.session)
                log = self.tryRequest(r, numTries=5, initialDelay=0.25, 
                                      scaleFactor=1.5)
            finally:
                kol.util.Report.addOutputSection("*")
            faxEvents = [event for event in log['entries'] 
                         if event['type'] == CLAN_LOG_FAX]
            lastEvent = None if len(faxEvents) == 0 else faxEvents[0]
            if lastEvent is None:
                self._lastFax = None
                self._lastFaxTime = None
                self._lastFaxUname = None
            else:
                self._lastFax = lastEvent['monster']
                lastFaxTimeAz = tz.localize(lastEvent['date'])
                lastFaxTimeUtc = lastFaxTimeAz.astimezone(utc)
                self._lastFaxTime = calendar.timegm(lastFaxTimeUtc.timetuple())
                self._lastFaxUname = lastEvent['userName']
            return lastEvent 

        
    def printLastFax(self):
        """ Get the chat text to represent what's in the Fax machine. """
        if utcTime() - self._lastFaxCheck >= self._checkFrequency:
            self.checkForNewFax(False)
        if self._lastFax is None:
            return "I can't tell what's in the fax machine."
        elapsed = utcTime() - self._lastFaxTime
        timeStr = "{} minutes".format(int((elapsed+59) // 60))
        return ("The fax has held a(n) {} for the last {}. "
                "(Send \"!fax list\" for a list of monsters.)"
                .format(self._lastFax, timeStr))
        
        
    def faxMonster(self, args, isPM):
        """Send a request, if not waiting on another request."""
        with self.__lock:
            (monster, force, message) = self.getFaxMatch(args)
            print monster
            print force
            print message
            matches = self._monsters.get(monster, [])
            print matches
            
            if monster is None or not matches:
                return message
            
            if isPM:
                str1 = "Matched {} ({})\n".format(matches[0].name, message)
                return str1 + "\n".join("/w {} {}"
                                        .format(m.faxbot.name, m.code)
                                                for m in matches)

            if self._delayMode.is_set():
                return ("Please wait {} more seconds to request a fax."
                        .format(int(self._faxWait 
                                    - time.time() + self._delayStart)))
            if self._requestQueue:
                return "I am still waiting on my last request."
            
            self._requestQueue.extend(matches)
            return "Requested {} ({})...".format(matches[0].name, message)
        
        
    def _processCommand(self, message, cmd, args):
        if cmd == "fax":
            if args.lower() == "list":
                return self._sendMonsterList(message['userId'])
            if args != "":
                isPM = (message['type'] == "private")
                return self.faxMonster(args, isPM)
            else:
                return self.printLastFax()
        with self.__lock:
            if self._faxState:
                if message.get('userId', 0) == self._faxState.requestId:
                    self.log("Received {} PM: {}".format(
                                                    self._faxState.requestId,
                                                    message['text']))
                    self._faxReply = message['text']
            return None
        
        
    def _sendMonsterList(self, uid):
        monsterList = sorted(set(self._monsters.keys()))
        # find monsters with common prefixes/postfixes
        # only groups monsters if one word is different
        splitMonsterNames = {name: name.split(' ') for name in monsterList}
        finalNames = []
        
        prefixPostfixMap = defaultdict(list)
        # here's the plan. Make a list of all prefixes and postfixes that a
        # monster belongs to. For example, "reanimated bat skeleton" belongs to
        # the following:
        #
        # reanimated XXXXXXXXXXXXXXX
        # reanimated bat XXXXXXXXXXX
        # XXXXXXXXXXXXXXXXX skeleton
        # XXXXXXXXXXXXX bat skeleton
        # reanimated XXXXXX skeleton
        for name, splitName in splitMonsterNames.items():
            if len(splitName) == 1:
                continue
            for prefixLength in range(len(splitName)):
                for postfixLength in range(len(splitName) - prefixLength):
                    if prefixLength != 0 or postfixLength != 0:
                        prefix = splitName[0:prefixLength]
                        postfix = [] if postfixLength == 0 else splitName[-postfixLength:]
                        prefixPostfixMap[(' '.join(prefix), ' '.join(postfix))].append(name)
        for k in prefixPostfixMap.keys():
            prefixPostfixMap[k] = list(set(prefixPostfixMap[k]))
        prefixPostfixMap = {k:v for k,v in prefixPostfixMap.items() if len(v) >= 2}
        
        while True:
            # compute highest character savings for each prefix/postfix pair
            prefixPostfixMap = {k:v for k,v in prefixPostfixMap.items() if len(v) >= 2}
            averagePrefixPostfixSavings = {}
            for pp, members in prefixPostfixMap.items():
                numMembers = sum(1 for entry in members if entry in splitMonsterNames)
                if numMembers < 2:
                    prefixPostfixSavings[pp] = -1
                    continue
                savings = len(pp[0]) + len(pp[1])
                savings += 1 if pp[0] else 0 # space before
                savings += 1 if pp[1] else 0 # space after
                savings -= 3 # " | " between entries
                savings *= numMembers - 1
                savings -= 2 # braces before and after
                averagePrefixPostfixSavings[pp] = float(savings) / numMembers
            if not averagePrefixPostfixSavings:
                break
            result = max(prefixPostfixSavings.items(), key=lambda x: x[1])
            if result[1] < 1:
                break # character savings is less than 1 character per item
            bestPP = result[0]
            preLen, postLen = map(len, bestPP[0])
            # get unique part of each monster
            cores = []
            for member in prefixPostfixMap[bestPP]:
                if member in splitMonsterNames and len(member) > preLen + postLen + 1:
                    cores.append(member[preLen:len(member)-postLen].strip())
                    del splitMonsterNames[member]
            cores.sort()
            finalNames.append("{}{}[{}]{}{}".format(bestPP[0], " " if bestPP[0] else "", 
                                                    " | ".join(cores), 
                                                    " " if bestPP[1] else "", bestPP[1]))
        finalNames.extend(splitMonsterNames.keys())
        
        # sort finalNames
        def finalNamesKey(x):
            if not x or x[0] != "[":
                return x
            idx = x.find(']')
            try:
                return x[idx+1:].strip()
            except IndexError:
                return x
                
        finalNames.sort(key=finalNamesKey)
        text = ("Available monsters:\n\n" +
                "\n".join(map(lambda x: "* " + x, finalNames)))
        self.sendKmail(Kmail(uid, text))
        return "Monster list sent."
    
    
    def _refreshMonsterList(self):
        valueCounter = lambda m: sum(1 for _ in chain.from_iterable(m.values()))
        entryCount = valueCounter(self._monsters)
        
        self.log("Updating xml... ({} entries)".format(entryCount))
        for _,v in self._monsters.items():
            v = [entry for entry in v
                 if entry.faxbot.xml in self._xmlAddresses.values()]
        
        # clear empty entries
        monsters = defaultdict(list)
        monsters.update(
                    {k:v for k,v in self._monsters.items() if v})
        self._monsters = monsters

        entryCount2 = valueCounter(self._monsters)
        if entryCount != entryCount2:
            self.log("Removed {} entries due to config file mismatch."
                      .format(entryCount - entryCount2))
        
        faxbots = set()
        loadingFailed = False
        numTries = 3
        for key in sorted(self._xmlAddresses.keys()):
            address = self._xmlAddresses[key]
            txt = None
            for _ in range(numTries):
                try:
                    txt = urlopen(address, timeout=self._timeout).read()
                    d = xmltodict.parse(txt)
                except (HTTPError, 
                        URLError, 
                        socket.timeout, 
                        socket.error,
                        ExpatError) as e:
                    self.log("Error loading webpage {} "
                             "for fax list: {}: {}"
                             .format(address, e.__class__.__name__, e.args))
                    loadingFailed = True
                else:
                    entryCount = valueCounter(self._monsters)
                    d1 = d[d.keys()[0]]
                    if 'botdata' not in d1:
                        self.log('Could not find botdata in xml '
                                  'from {}:\n{}'
                                  .format(address, txt))
                        continue
                    faxbot = _Faxbot(d1['botdata']['name'].encode('ascii'), 
                                     int(d1['botdata']['playerid']),
                                     address)
                    faxbots.add(faxbot)
                    monsters = d1['monsterlist']['monsterdata']
                    newMonsters = {}
                    for monster in monsters:
                        mname = unidecode(monster['actual_name']).lower()
                        code = unidecode(monster['command']).lower()
                        name = unidecode(monster['name'])
                        newMonsters[mname] = FaxMonsterEntry(name, 
                                                             code, 
                                                             faxbot)
                        for n,alias in self._alias.items():
                            if n.lower().strip() in [mname, 
                                                     code, 
                                                     name.lower().strip()]:
                                newMonsters[mname].addAlias(alias)

                    for k,v in self._monsters.items():
                        self._monsters[k] = [entry for entry in v
                                             if entry.faxbot.xml != address]
                    for mname,monster in newMonsters.items():
                        self._monsters[mname].append(monster)
                    entryCount2 = valueCounter(self._monsters)
                    
                    # clear empty entries
                    monsters = defaultdict(list)
                    monsters.update(
                                {k:v for k,v in self._monsters.items() if v})
                    self._monsters = monsters
                    self.log("Net change of {} entries from {} xml ({} -> {})"
                              .format(entryCount2 - entryCount, 
                                      faxbot.name,
                                      entryCount,
                                      entryCount2))
                    break

        self._lastXmlUpdate = time.time()
        if not loadingFailed:
            oldLength = valueCounter(self._monsters)
            self._monsters = {k: [val for val in v if val.faxbot in faxbots] for k,v in self._monsters.items()}
            self._monsters = {k: v for k,v in self._monsters.items() if v}
            newLength = valueCounter(self._monsters)
            if oldLength > newLength:
                self.log("Removed {} entries that referred to faxbots that are no longer used".format(oldLength - newLength))
        

    def _heartbeat(self):
        if self._finishInitialization.is_set():
            self._finishInitialization.clear()
            self._refreshMonsterList()
            self._initialized = True
        if self._initialized:
            with self.__lock:
                # are we waiting for a request?
                if self._faxState:
                    # check if we received a reply
                    request = self._requestQueue[0]
                    if self._faxReply:
                        # check if it matches
                        regex = self._success[
                                        ''.join(request.faxbot.name.lower())]
                        
                        if re.search(regex, self._faxReply):
                            # matched!
                            self.chat("{} has delivered a(n) {}."
                                      .format(request.faxbot.name,
                                              request.name))
                            self._requestQueue.clear()
                            self._delayMode.set()
                            self._delayStart = time.time()
                            self._faxCommands = []
                        else:
                            # not a match.
                            self.chat("{} reply: {}"
                                      .format(request.faxbot.name,
                                              self._faxReply))
                            self._requestQueue.popleft()
                            if not self._requestQueue:
                                self.chat("Could not receive fax. "
                                          "Try one of: {}"
                                          .format(", "
                                                .join(self._faxCommands)))
                                self._faxCommands = []
                        self._faxReply = None
                        self._faxState = None
                    else:
                        # no fax reply yet
                        if (time.time() - self._faxState.requestTime
                                > self._abortTime):
                            self.chat("{} did not reply.".format(
                                        request.faxbot.name))
                            self._requestQueue.popleft()
                            self._faxState = None
                            self._faxReply = None
                            if not self._requestQueue:
                                self.chat("Could not receive fax. "
                                          "Try one of: {}"
                                          .format(", "
                                                .join(self._faxCommands)))
                                self._faxCommands = []
                
                elif self._delayMode.is_set():
                    if time.time() - self._delayStart > self._faxWait:
                        self._delayMode.clear()
                        self._delayStart = 0

                elif self._requestQueue:
                    request = self._requestQueue[0]
                    self.chat("Requesting {} from {}..."
                              .format(request.name,
                                      request.faxbot.name))
                    self._faxState = _FaxState(requestTime=time.time(),
                                               requestId=request.faxbot.id)
                    self.whisper(request.faxbot.id, request.code)
                    self._faxCommands.append("/w {} {}".format(
                                                    request.faxbot.name,
                                                    request.code))
                elif time.time() - self._lastXmlUpdate > 60 * self._xmlMins:
                    self._refreshMonsterList()


    def _eventCallback(self, eData):
        s = eData.subject
        if s == "state":
            if eData.to is None:
                self._eventReply({
                        'warning': '(omitted for general state inquiry)'})
            else:
                self._eventReply(self.state)
            
    
    def _availableCommands(self):
        return {'fax': "!fax: check the contents of the fax machine. "
                       "'!fax MONSTERNAME' requests a fax from FaxBot."}
