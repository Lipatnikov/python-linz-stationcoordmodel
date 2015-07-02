import sys
import os.path
from datetime import datetime
from collections import namedtuple
import json
from ConfigParser import SafeConfigParser
import numpy as np
import pandas as pd
from pandas.tseries.offsets import DateOffset
from LINZ.Geodetic.Ellipsoid import GRS80
from LINZ.Geodetic import GDB
from LINZ import CORS_Timeseries
from LINZ.DeformationModel.Model import Model as DeformationModel
from LINZ.DeformationModel.ITRF_NZGD2000 import Transformation as ITRF_Transformation
from LINZ.StationCoordinateModel import Model as StationCoordinateModel
# from StationCoordinateModel import Model as StationCoordinateModel

StationData=namedtuple('StationData','code timeseries stationCoordModel scmTimeseries gdbTimeseries')
StationOffsetStats=namedtuple('StationOffsetStats','date gdbOffset scmOffset')

class CORS_Analyst( object ):

    refconfigfile=os.path.splitext(__file__)[0]+'.cfg'
    configfile=os.path.basename(refconfigfile)
    configgroup='analysis_settings'

    def __init__( self, verbose=False, write_plot_files=False, configfile=None, configgroup=None ):
        self._verbose=verbose
        self._writecsv=write_plot_files
        self._configfile=configfile or CORS_Analyst.configfile
        self._configgroup=configgroup or CORS_Analyst.configgroup

        if not os.path.exists(self._configfile):
            sys.stderr.write("Cannot find configuration file {0}\n".format(self._configfile))
            sys.exit()

        if self._verbose:
            print "Reading configuration from",self._configfile

        cfg=SafeConfigParser()
        cfg.readfp(open(self._configfile))
        self._cfg=cfg
        self._tsdb=cfg.get(self._configgroup,'timeseries_database')
        if not os.path.exists(self._tsdb):
            raise RuntimeError('Time series database '+self._tsdb+' does not exist')
        self._tssolutiontype=cfg.get(self._configgroup,'timeseries_solution_type')
        self._scmpath=cfg.get(self._configgroup,'station_coordinate_model_path')
        self._offsetdays=cfg.getint(self._configgroup,'offset_calc_ndays')
        self._teststat=cfg.get(self._configgroup,'offset_test_stat')
        self._gdbwarning=self.loadWarningLevels('gdb_offset_warning')
        self._scmwarning=self.loadWarningLevels('scm_offset_warning')

        self.loadDeformationModel()

    def loadWarningLevels( self, warning_type ):
        try:
            levels=[float(x) for x in self._cfg.get(self._configgroup,warning_type ).split()]
            if len(levels) < 2: levels.append[levels[0]]
        except:
            raise RuntimeError('Invalid value for '+warning_type+' configuration item')
        return levels

    def loadDeformationModel(self):
        path=self._cfg.get(self._configgroup,'deformation_model_path')
        mod = DeformationModel(path)
        self.deformationModelVersion=mod.version()
        self._itrftrans=ITRF_Transformation( modeldir=path, toNZGD2000=False, itrf='ITRF2008')

    def gdbTimeseries( self, code, dates, xyz0=None ):
        markdata=GDB.get(code)
        lon,lat,hgt=markdata.official_coordinate
        markitrf=[GRS80.xyz(self._itrftrans(lon,lat,hgt,d)) for d in dates]
        gdbts=CORS_Timeseries.Timeseries(code,solutiontype='gdb',dates=dates,xyz=markitrf,xyz0=xyz0)
        return gdbts

    def scmTimeseries( self, model, dates ):
        spmxyz=model.calc(dates.to_pydatetime(),enu=False)
        code=model.station
        xyz0=model.xyz
        scmts=CORS_Timeseries.Timeseries(code,solutiontype='scm',dates=dates,xyz=spmxyz,xyz0=xyz0)
        return scmts

    def getStationData( self, code ):
        # Load the time series
        ts=CORS_Timeseries.SqliteTimeseries(self._tsdb,code,solutiontype=self._tssolutiontype)

        # Load the station coordinate model corresponding to the station
        scmfile=self._scmpath.replace('{code}',code)
        model=StationCoordinateModel(filename=scmfile)

        # Get the reference coordinate used by the model and apply to the time series
        # (so that we get compatible xyz errors)

        xyz0=model.xyz
        ts.setXyzTransform(xyz0=xyz0)
        dates=ts.dates()

        # Set up a time series of based on the station coordinate model
        spmts=self.scmTimeseries( model, dates )

        # Set up a time series based on the GDB official coordinate and the deformation
        # model
        gdbts=self.gdbTimeseries( code,dates,xyz0)

        return StationData(code,ts,model,spmts,gdbts)

    def getStationOffsets( self, stnData, ndays ):
        # Calculate mean offsets the last nDays data

        dates=stnData.timeseries.dates()

        refdate=dates[-1]+DateOffset(days=-ndays)
        calcdate=refdate+DateOffset(days=ndays/2)
        select=dates > refdate
        obsts=stnData.timeseries.getData()[select]
        gdbts=stnData.gdbTimeseries.getData()[select]
        scmts=stnData.scmTimeseries.getData()[select]
        return StationOffsetStats(calcdate,(obsts-gdbts).describe(),(obsts-scmts).describe())

    def writeTimeseriesCSV( self, code, tsdata, csv_cfg_type ):
        results_dir=self._cfg.get(self._configgroup,'result_path')
        results_file=self._cfg.get(self._configgroup,csv_cfg_type)
        csv_file=os.path.join(results_dir,results_file.replace('{code}',code))
        tsdata.to_csv(csv_file,float_format="%.4f",index_label='epoch')

    def writeResultsJSON( self, results ):
        results_dir=self._cfg.get(self._configgroup,'result_path')
        results_file=self._cfg.get(self._configgroup,'summary_file')
        json_file=os.path.join(results_dir,results_file)
        with open(json_file,'w') as jf:
            oldencoder=json.encoder.FLOAT_REPR
            try:
                json.encoder.FLOAT_REPR=lambda f: format(f,'.4f')
                jf.write(json.dumps(results,indent=2,sort_keys=True))
            finally:
                json.encoder.FLOAT_REPR=oldencoder

    def trimmedModelTimeseries( self, tsdata, precision ):
        enu=np.vstack((tsdata.e,tsdata.n,tsdata.u)).T
        timevar=tsdata.index.to_pydatetime()
        reftime=timevar[0]
        timevar=np.array([(t-reftime).total_seconds()/(60*60*24.0) for t in timevar])
        usepoint=[False]*len(timevar)
        precision *= precision

        def addPoints( start, end, usepoint ):
            usepoint[start]=True
            usepoint[end]=True
            if end <= start+1:
                return

            timediff=timevar[end]-timevar[start]
            diff=(enu[end]-enu[start]).reshape(1,3)
            offset=timevar[start:end+1].reshape(end+1-start,1).dot(diff)
            offset=enu[start:end+1]-enu[start,:]-offset/timediff
            offset=np.square(offset).sum(axis=1)
            i=np.argmax(offset)
            if i > 0 and i < end-start and offset[i] > precision:
                addPoints(start,start+i,usepoint)
                addPoints(start+i,end,usepoint)

        addPoints(0,len(usepoint)-1,usepoint)
        return tsdata[usepoint]

    def compileOffsetStats( self, code, stats, teststat, levels, testtype ):
        results={}
        for m in 'count mean std median min max'.split():
            mloc='50%' if m == 'median' else m
            mstats=stats.loc[mloc]
            results[m]=[mstats.e,mstats.n,mstats.u]
        results['count']=results['count'][0]
        teststats=results[teststat]
        testh=np.hypot(teststats[0],teststats[1])
        testv=abs(teststats[2])
        status='warn' if testh > levels[0] or testv > levels[1] else 'good'
        message=None
        if status=='warn':
            message=''
            if testh > levels[0]:
                message=(message+
                         "{0} {1} {2} horizontal offset {3:.4f} m exceeds tolerance {4:.4f} m\n"
                         .format(code,testtype,teststat,testh,levels[0]))
            if testv > levels[1]:
                message=(message+
                         "{0} {1} {2} vertical offset {3:.4f} m exceeds tolerance {4:.4f} m \n"
                         .format(code,testtype,teststat,testv,levels[1]))
            if self._verbose:
                print "  {0} coordinates are significantly offset ({1:.4f} {2:.4f} m)".format(
                    testtype,testh,testv)

        results['status']=status
        results['status_message']=message
        return results

    def compileStationData( self, code ):
        if self._verbose:
            print "Compiling data for",code
        stndata=self.getStationData( code )
        offsets=self.getStationOffsets( stndata, self._offsetdays )
        stnresults={
            "code": code,
            "offset_test_date": offsets.date.strftime("%Y-%m-%d"),
            "gdb_offset": self.compileOffsetStats(code,offsets.gdbOffset,self._teststat,self._gdbwarning,'GDB coordinate'),
            "scm_offset": self.compileOffsetStats(code,offsets.scmOffset,self._teststat,self._scmwarning,'station coordinate model'),
            "scm_version": stndata.stationCoordModel.versiondate.strftime("%Y-%m-%d %H:%M:%S"),
            }
        
        if self._writecsv:
            ts=stndata.timeseries
            tsdata=ts.getData()
            self.writeTimeseriesCSV( code, tsdata, 'timeseries_csv' )

            # Generate data for plotting gdb coord and stn pred model

            trimmodel=self._cfg.get(self._configgroup,'trim_model_timeseries').lower() == 'yes'
            if trimmodel:
                plotmargin=self._cfg.getint(self._configgroup, 'plot_margin')
                startdate=tsdata.index[0]-DateOffset(days=plotmargin)
                enddate=tsdata.index[-1]+DateOffset(days=plotmargin)
                dates=pd.date_range(startdate,enddate,freq='d')
                xyz0=ts.xyz0()
                precision=self._cfg.getfloat(self._configgroup,'trim_model_precision')

                gdbmodel=self.gdbTimeseries(code,dates,xyz0)
                gdbmodel=self.trimmedModelTimeseries(gdbmodel.getData(),precision)

                scmmodel=self.scmTimeseries(stndata.stationCoordModel,dates)
                scmmodel=self.trimmedModelTimeseries(scmmodel.getData(),precision)
            else:
                gdbmodel=stndata.gdbTimeseries.getData()
                scmmodel=stndata.scmTimeseries.getData()

            self.writeTimeseriesCSV(code,gdbmodel,'gdb_timeseries_csv')
            self.writeTimeseriesCSV(code,scmmodel,'scm_timeseries_csv')

        return stnresults

    def compileAllStationData( self, codelist=None ):
        if codelist is None or len(codelist) == 0:
            codelist=self._cfg.get(self._configgroup, 'test_stations').split()

        results={
            "calcdate": datetime.now().strftime("%Y-%m-%d: %H:%M:%S"),
            "deformation_model_version": self.deformationModelVersion,
            }
        coderesults=[]
        for c in sorted(codelist):
            try:
                coderesults.append(self.compileStationData(c))
            except:
                print "Station "+c+": "+str(sys.exc_info()[1])
        results['station_summary']=coderesults
        self.writeResultsJSON(results)
        return results

def main():
    import argparse
    parser=argparse.ArgumentParser(description="Analyse PositioNZ CORS data to monitor datum integrity. "+
                                  "Results of analysis are written to files specified in the configuration file",
                                   usage="%(prog)s [options] code code ...")
    parser.add_argument('code',nargs='*',help="Specific station codes to analyse.  Default is as defined in the configuration file")
    parser.add_argument('-c','--config-file',help="Name of the configuration file")
    parser.add_argument('-v','--verbose',action='store_true',help="Verbose output")
    parser.add_argument('-p','--plot-files',action='store_true',help="Generate CSV files for plotting")
    parser.add_argument('-d','--dump-config-file',action='store_true',help="Print a sample configuration file")
    args=parser.parse_args()

    if args.dump_config_file:
        with open(CORS_Analyst.refconfigfile) as cf:
            for l in cf:
                print(l.strip())
        sys.exit()

    codes=[c.upper() for c in args.code]

    a=CORS_Analyst(configfile=args.config_file,verbose=args.verbose,write_plot_files=args.plot_files)
    a.compileAllStationData(codes)

if __name__ == "__main__":
    main()