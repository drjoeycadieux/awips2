##
# This software was developed and / or modified by Raytheon Company,
# pursuant to Contract DG133W-05-CQ-1067 with the US Government.
# 
# U.S. EXPORT CONTROLLED TECHNICAL DATA
# This software product contains export-restricted data whose
# export/transfer/disclosure is restricted by U.S. law. Dissemination
# to non-U.S. persons whether in the United States or abroad requires
# an export license or other authorization.
# 
# Contractor Name:        Raytheon Company
# Contractor Address:     6825 Pine Street, Suite 340
#                         Mail Stop B8
#                         Omaha, NE 68106
#                         402.291.0100
# 
# See the AWIPS II Master Rights File ("Master Rights File.pdf") for
# further licensing information.
##
#
# Code mostly separated from legacy VTECDecoder.py
#  
#    
#     SOFTWARE HISTORY
#    
#    Date            Ticket#       Engineer       Description
#    ------------    ----------    -----------    --------------------------
#    06/11/13        #2083         randerso       Log active table changes, save backups
#    03/06/14        #2883         randerso       Pass siteId into mergeFromJava
#    03/25/14        #2884         randerso       Added xxxid to VTECChange
#    05/15/14        #3157         dgilling       Support multiple TPC and SPC sites.
#    06/17/13        #3296         randerso       Moved active table backup and purging 
#                                                 to a separate thread in java.
#                                                 Added performance logging
#    02/05/15        #4099         randerso       Changed log level of year-end issuance tweak
#    Apr 25, 2015     4952         njensen        Updated for new JEP API
#    May 22, 2015     4522         randerso       Create proper primary key for ActiveTableRecord
#    06/20/16         5709         dgilling       Use TropicalCycloneUtil to bin tropical storms
#                                                 when comparing records for merge.
#    08/03/16         5747         dgilling       Move edex_static to common_static.
#    09/01/16         5872         dgilling       Fix error-handling in previous revision.
#    01/31/18         20537        ryu            Fix issuetime assignment to prior year for EXB.
#

##
# This is a base file that is not intended to be overridden.
##



import time
import copy
import os
import VTECTableUtil, VTECTableSqueeze, VTECPartners
import TropicalCycloneUtil
import LogStream, ActiveTableVtec, ActiveTableRecord
import JUtil
from java.util import ArrayList
from com.raytheon.uf.common.localization import PathManagerFactory
from com.raytheon.uf.common.localization import LocalizationContext
LocalizationType = LocalizationContext.LocalizationType
LocalizationLevel = LocalizationContext.LocalizationLevel
from com.raytheon.uf.common.activetable import VTECPartners as JavaVTECPartners

from com.raytheon.uf.common.time.util import TimeUtil
from com.raytheon.uf.common.status import PerformanceStatus
perfStat = PerformanceStatus.getHandler("ActiveTable")

class ActiveTable(VTECTableUtil.VTECTableUtil):
    
    def __init__(self, activeTableMode, logger=None):
        self._time = time.time()
        self._logger = logger

        # create a dummy name to simplify the file access code in VTECTableUtil
        pathMgr = PathManagerFactory.getPathManager()
        siteCx = pathMgr.getContext(LocalizationType.COMMON_STATIC, LocalizationLevel.SITE)
        filePath = pathMgr.getFile(siteCx,"vtec").getPath()
        VTECTableUtil.VTECTableUtil.__init__(self, os.path.join(filePath, activeTableMode + ".tbl"))

    def updateActiveTable(self, activeTable, newRecords, offsetSecs=0):
        #merges the previous active table and new records into a new table.
        #Returns:
        #  (updated active table, purged records, changesforNotify, changeFlag)
        updatedTable = []
        changes = []
        changedFlag = False

        #delete "obsolete" records from the old table.
        timer = TimeUtil.getTimer()
        timer.start()
        vts = VTECTableSqueeze.VTECTableSqueeze(self._time+offsetSecs)
        activeTable, tossRecords = vts.squeeze(activeTable)
        for r in tossRecords:
            r['state'] = "Purged"
        del vts
        if len(tossRecords):
            changedFlag = True
        timer.stop();
        perfStat.logDuration("updateActiveTable squeeze", timer.getElapsedTime());

        #expand out any 000 UGC codes, such as FLC000, to indicate all
        #zones.
        timer.reset()
        timer.start()
        newRecExpanded = []
        compare1 = ['phen', 'sig', 'officeid', 'etn', 'pil']
        for newR in newRecords:
            if newR['id'][3:6] == "000":
                for oldR in activeTable:
                    if self.hazardCompare(oldR, newR, compare1) and \
                      oldR['id'][0:2] == newR['id'][0:2] and \
                      (oldR['act'] not in ['EXP', 'CAN', 'UPG'] or \
                       oldR['act'] == 'EXP' and oldR['endTime'] > newR['issueTime']):
                        newE = copy.deepcopy(newR)
                        newE['id'] = oldR['id']
                        newRecExpanded.append(newE)
            else:
                newRecExpanded.append(newR)
        newRecords = newRecExpanded
        timer.stop();
        perfStat.logDuration("updateActiveTable expand", timer.getElapsedTime());

        # match new records with old records, with issue time is different
        # years and event times overlap. Want to reassign ongoing events
        # from last year's issueTime to be 12/31/2359z, rather than the
        # real issuetime (which is this year's).        
        timer.reset()
        timer.start()
        compare = ['phen', 'sig', 'officeid', 'pil', 'etn']
        for newR in newRecords:
            # not needed for NEWs
            if newR['act'] == 'NEW':
                continue

            lastyear = time.gmtime(newR['issueTime']).tm_year - 1 # year prior to issuance time
            for oldR in activeTable:
                if self.hazardCompare(oldR, newR, compare) and \
                        time.gmtime(oldR['issueTime']).tm_year == lastyear:
                    if (newR['act'] in ["EXP", "CAN", "UPG"] and \
                            newR['endTime'] == oldR['endTime']) or \
                            self.__overlaps((oldR['startTime'],oldR['endTime']), \
                                            (newR['startTime'],newR['endTime'])) or \
                            (newR['act'] == "EXB" and \
                                 (oldR['startTime'] == newR['endTime'] or \
                                  oldR['endTime'] == newR['startTime'])):
                        LogStream.logEvent("Reset issuance time to last year:",
                                           "\nNewRec: ", self.printEntry(newR),
                                           "OldRec: ", self.printEntry(oldR))
                        newR['issueTime'] = time.mktime((lastyear, 12, 31, 23, 59,
                                                         0, -1, -1, -1))
                        break

        timer.stop();
        perfStat.logDuration("updateActiveTable match", timer.getElapsedTime());

        # split records out by issuance year for processing
        timer.reset()
        timer.start()
        newRecDict = {}   #key is issuance year
        oldRecDict = {}
        years = []
        for newR in newRecords:
            issueYear = time.gmtime(newR['issueTime']).tm_year
            records = newRecDict.get(issueYear, [])
            records.append(newR)
            newRecDict[issueYear] = records
            if issueYear not in years:
                years.append(issueYear)
        for oldR in activeTable:
            issueYear = time.gmtime(oldR['issueTime']).tm_year
            records = oldRecDict.get(issueYear, [])
            records.append(oldR)
            oldRecDict[issueYear] = records
            if issueYear not in years:
                years.append(issueYear)
        timer.stop();
        perfStat.logDuration("updateActiveTable split", timer.getElapsedTime());
    
        # process each year
        timer.reset()
        timer.start()
        compare = ['id', 'phen', 'sig', 'officeid', 'pil']

        for year in years:
            newRecords = newRecDict.get(year,[])
            oldRecords = oldRecDict.get(year,[])

            # now process the old and new records
            for oldR in oldRecords:

                keepflag = 1
                for newR in newRecords:

                    if newR['act'] == "ROU":
                        continue

                    if self.hazardCompare(oldR, newR, compare):
                        #we don't keep older records with same etns
                        if newR['etn'] == oldR['etn']:
                            keepflag = 0   #don't bother keeping this record
                            break
                        
                        # special case for tropical storms
                        # ensure we have the same "class" of tropical storm
                        # class is determined by ETN
                        if newR['phen'] in TropicalCycloneUtil.TROPICAL_PHENS:
                            oldRecBasin = None
                            newRecBasin = None
                            try:
                                oldRecBasin = TropicalCycloneUtil.get_tropical_storm_basin(oldR)
                            except ValueError:
                                LogStream.logProblem("Tropical Hazard record has invalid ETN: ", self.printEntry(oldR))
                                continue
                            try:
                                newRecBasin = TropicalCycloneUtil.get_tropical_storm_basin(newR)
                            except ValueError:
                                LogStream.logProblem("Tropical Hazard record has invalid ETN: ", self.printEntry(newR))
                                continue
                            if not newRecBasin or not oldRecBasin or newRecBasin != oldRecBasin:
                                continue

                        #higher etns
                        elif newR['etn'] > oldR['etn']:
                            #only keep older etns if end time hasn't passed
                            #or old record is UFN and CAN:
                            ufn = oldR.get('ufn', 0)
                            if self._time > oldR['endTime'] or \
                              (oldR['act'] == "CAN" and ufn) or \
                              oldR['act'] in ['EXP','UPG','CAN']:
                                keepflag = 0
                                break

                        #lower etns, ignore (keep processing)

                if keepflag == 0:
                    oldR['state'] = "Replaced"
                    changedFlag = True
                updatedTable.append(oldR)
        timer.stop();
        perfStat.logDuration("updateActiveTable process", timer.getElapsedTime());

        #always add in the new records (except for ROU)
        timer.reset()
        timer.start()
        compare = ['id', 'phen', 'sig', 'officeid', 'pil', 'etn']
        for year in newRecDict.keys():
            newRecords = newRecDict[year]
            for newR in newRecords:
                if newR['act'] != "ROU":

                    #for COR, we need to find the original action, and 
                    #substitute it.
                    if newR['act'] == "COR":
                        for rec in updatedTable:
                            if self.hazardCompare(rec, newR, compare):
                                LogStream.logVerbose(\
                                  "COR record matched with:",
                                  "\nNewRec: ", self.printEntry(newR),
                                  "OldRec: ", self.printEntry(rec),
                                  "\nReassign action to: ", rec['act'])
                                newR['act'] = rec['act']
                                rec['state'] = "Replaced"
                                break
                        #due to above code, this should never execute
                        if newR['act'] == "COR":
                            LogStream.logProblem("COR match not found for:\n",
                              self.printEntry(newR), "\nRecord discarded.")

                    if newR['act'] != "COR":
                        updatedTable.append(newR)
                        changedFlag = True

                        #determine changes for notifications
                        rec = (newR['officeid'], newR['pil'], newR['phensig'], newR['xxxid'])
                        if rec not in changes:
                            changes.append(rec)

        timer.stop();
        perfStat.logDuration("updateActiveTable add", timer.getElapsedTime());

        #filter out any captured text and overviewText if not in the categories
        timer.reset()
        timer.start()
        cats = self._getTextCaptureCategories()
        if cats is not None:
            for rec in updatedTable:
                if rec['pil'] not in cats:
                    if rec.has_key('segText'):
                        del rec['segText']
                    if rec.has_key('overviewText'):
                        del rec['overviewText']

        timer.stop();
        perfStat.logDuration("updateActiveTable filter", timer.getElapsedTime());
        return updatedTable, tossRecords, changes, changedFlag

    # time overlaps, if tr1 overlaps tr2 (adjacent is not an overlap)
    def __overlaps(self, tr1, tr2):
        if self.__containsT(tr2, tr1[0]) or self.__containsT(tr1, tr2[0]):
            return 1
        return 0
    
    def __containsT(self, tr, t):
        return (t >= tr[0] and t < tr[1])
    
    def _getTextCaptureCategories(self):
        #gets the list of product categories that need their text captured.
        #if the list is empty, then all products are captured into the
        #active table and None is returned.
        cats = getattr(VTECPartners, "VTEC_CAPTURE_TEXT_CATEGORIES", [])
        if len(cats) == 0:
            return None
        LogStream.logDebug("Text Capture Categories: ", cats)
        return cats
    
    def activeTableMerge(self, activeTable, newRecords, offsetSecs=0):
        
        updatedTable, purgedRecords, changes, changedFlag = self.updateActiveTable(activeTable, newRecords, offsetSecs)
        
         #strip out the "state" field
        outTable = []        
        for r in updatedTable:
            if r['state'] == "Purged":
                purgedRecords.append(r)                
            else:
                outTable.append(r)

        return outTable, purgedRecords, changes, changedFlag

def mergeFromJava(siteId, activeTable, newRecords, logger, mode, offsetSecs=0):
    perfStat.log("mergeFromJava called for site: %s, activeTable: %d , newRecords: %d" %
                 (siteId, activeTable.size(), newRecords.size()))
    timer = TimeUtil.getTimer()
    timer.start()
    pyActive = []
    szActive = activeTable.size()
    for i in range(szActive):
        pyActive.append(ActiveTableRecord.ActiveTableRecord(activeTable.get(i), "Previous"))
    
    pyNew = []
    szNew = newRecords.size()
    for i in range(szNew):
        rec = ActiveTableRecord.ActiveTableRecord(newRecords.get(i))
        pyNew.append(rec)
    
    active = ActiveTable(mode, logger)
    
    logger.info("Updating " + mode + " Active Table: new records\n" +
       active.printActiveTable(pyNew, combine=1))

    timer.stop()
    perfStat.logDuration("mergeFromJava preprocess", timer.getElapsedTime());
    
    updatedTable, purgeRecords, changes, changedFlag = active.activeTableMerge(pyActive, pyNew, offsetSecs)
    perfStat.log("mergeFromJava activeTableMerge returned updateTable: %d, purgeRecords: %d, changes: %d" %
                 (len(updatedTable), len(purgeRecords), len(changes)))
    
    timer.reset()
    timer.start()
    logger.info("Updated " + mode + " Active Table: purged\n" +
       active.printActiveTable(purgeRecords, combine=1))

    stateDict = {}
    for r in updatedTable:
        recs = stateDict.get(r['state'], [])
        recs.append(r)
        stateDict[r['state']] = recs

    keys = stateDict.keys()
    keys.sort()
    for key in keys:
        if key == "Previous":
            continue
        
        logger.info("Updated " + mode + " Active Table: "+ key +"\n" +
            active.printActiveTable(stateDict[key],  combine=1))

    updatedList = ArrayList(len(updatedTable))
    for r in updatedTable:
        if r['state'] not in ["Previous", "Replaced"]:
            updatedList.add(r.javaRecord())
    
    purgedList = ArrayList(len(purgeRecords))
    for r in purgeRecords:
        purgedList.add(r.javaRecord())
    
    changeList = ArrayList(len(changes))
    if (changedFlag):
        from com.raytheon.uf.common.activetable import VTECChange
        for c in changes:
            changeList.add(VTECChange(c[0],c[1],c[2],c[3]))

    from com.raytheon.uf.common.activetable import MergeResult
    result = MergeResult(updatedList, purgedList, changeList)
    timer.stop()
    perfStat.logDuration("mergeFromJava postprocess", timer.getElapsedTime());
    return result
