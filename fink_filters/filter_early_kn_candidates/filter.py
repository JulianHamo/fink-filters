# Copyright 2019-2020 AstroLab Software
# Author: Juliette Vlieghe
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from pyspark.sql.functions import pandas_udf, PandasUDFType
from pyspark.sql.types import BooleanType

import numpy as np
import pandas as pd
import requests, logging
import os

from astropy.coordinates import SkyCoord
from astropy import units as u

from fink_science.conversion import dc_mag
    

@pandas_udf(BooleanType(), PandasUDFType.SCALAR)
def early_kn_candidates(objectId, drb, classtar, jd, jdstarthist, ndethist, 
                cdsxmatch, fid, magpsf, sigmapsf, magnr, sigmagnr, magzpsci, 
                isdiffpos, ra, dec, mangrove_path=None) -> pd.Series:
    """ Return alerts considered as KN candidates.
    If the environment variable KNWEBHOOK is defined and match a webhook url,
    the alerts that pass the filter will be sent to the matching Slack channel.
    
    Parameters
    ----------
    objectId: Spark DataFrame Column
        Column containing the alert IDs
    drb: Spark DataFrame Column
        Column containing the Deep-Learning Real Bogus score
    classtar: Spark DataFrame Column
        Column containing the sextractor score
    jd: Spark DataFrame Column
        Column containing observation Julian dates at start of exposure [days]
    jdstarthist: Spark DataFrame Column
        Column containing earliest Julian dates of epoch corresponding to ndethist [days]
    ndethist: Spark DataFrame Column
        Column containing the number of prior detections (with a theshold of 3 sigma)
    cdsxmatch: Spark DataFrame Column
        Column containing the cross-match values
    fid: Spark DataFrame Column
        Column containing filter, 1 for green and 2 for red
    magpsf,sigmapsf: Spark DataFrame Columns
        Columns containing magnitude from PSF-fit photometry, and 1-sigma error
    magnr,sigmagnr: Spark DataFrame Columns
        Columns containing magnitude of nearest source in reference image PSF-catalog
        within 30 arcsec and 1-sigma error
    magzpsci: Spark DataFrame Column
        Column containing magnitude zero point for photometry estimates
    isdiffpos: Spark DataFrame Column
        Column containing:
        t or 1 => candidate is from positive (sci minus ref) subtraction;
        f or 0 => candidate is from negative (ref minus sci) subtraction
    ra: Spark DataFrame Column
        Column containing the right Ascension of candidate; J2000 [deg]
    dec: Spark DataFrame Column
        Column containing the declination of candidate; J2000 [deg]
    magpsf: Spark DataFrame Column
        Column containing the magnitude from PSF-fit photometry [mag]
    mangrove_path: Spark DataFrame Column, optional
        Path to the Mangrove file. Default is None, in which case
        `data/mangrove_filtered.csv` is loaded.
    
    Returns
    ----------
    out: pandas.Series of bool
        Return a Pandas DataFrame with the appropriate flag:
        false for bad alert, and true for good alert.
    """
    
    high_drb = drb.astype(float) > 0.5
    high_classtar = classtar.astype(float) > 0.4
    new_detection = jd.astype(float) - jdstarthist.astype(float) < 0.25
    
    list_simbad_galaxies = [
        "galaxy",
        "Galaxy",
        "EmG",
        "Seyfert",
        "Seyfert_1",
        "Seyfert_2",
        "BlueCompG",
        "StarburstG",
        "LSB_G",
        "HII_G",
        "High_z_G",
        "GinPair",
        "GinGroup",
        "BClG",
        "GinCl",
        "PartofG",
    ]
    
    keep_cds = \
        ["Unknown", "Transient","Fail"] + list_simbad_galaxies

    f_kn = high_drb & high_classtar & new_detection
    f_kn = f_kn & cdsxmatch.isin(keep_cds)
        
    #cross match with Mangrove catalog. Distances are in Mpc
    if f_kn.any():
        # dc magnitude (apparent)
        mag, err_mag = np.array([
            dc_mag(i[0], i[1], i[2], i[3], i[4], i[5], i[6])
            for i in zip(
                np.array(fid[f_kn]),
                np.array(magpsf[f_kn]),
                np.array(sigmapsf[f_kn]),
                np.array(magnr[f_kn]),
                np.array(sigmagnr[f_kn]),
                np.array(magzpsci[f_kn]),
                np.array(isdiffpos[f_kn]))
        ]).T
        # mangrove catalog
        if mangrove_path is not None:
            pdf_mangrove = pd.read_csv(mangrove_path.values[0])
        else:
            curdir = os.path.dirname(os.path.abspath(__file__))
            mangrove_path = curdir + '/data/mangrove_filtered.csv'
            pdf_mangrove = pd.read_csv(mangrove_path)
        catalog_mangrove = SkyCoord(
            ra =np.array(pdf_mangrove.ra, dtype=np.float) * u.degree,
            dec=np.array(pdf_mangrove.dec, dtype=np.float) * u.degree
        )
        
        pdf = pd.DataFrame.from_dict({'fid':fid[f_kn],'ra':ra[f_kn],'dec':dec[f_kn],
                                      'mag':mag,'err_mag':err_mag})
        # identify galaxy somehow close to each alert
        idx_mangrove,idxself,_,_=SkyCoord(ra = pdf.ra*u.degree, dec = pdf.dec*u.degree)\
            .search_around_sky(catalog_mangrove, 2*u.degree)
        
        # cross match
        galaxy_matching=[]
        for i,row in enumerate(pdf.itertuples()):
            idx_reduced = idx_mangrove[idxself==i]
            abs_mag = row.mag-1-5*np.log10(pdf_mangrove.loc[idx_reduced,:].lum_dist)
            # cross-match on position. We take a radius of 50 kpc
            galaxy_matching.append(((SkyCoord(
                ra = row.ra*u.degree, 
                dec = row.dec*u.degree
            ).separation(catalog_mangrove[idx_reduced]).radian<0.05/pdf_mangrove.loc[idx_reduced,:].ang_dist)
            # absolute magnitude
            & (abs_mag>15) & (abs_mag<17)
            ).any())
        
        f_kn[f_kn] = galaxy_matching
        
    if 'KNWEBHOOK_MANGROVE' in os.environ:
        for alertID in objectId[f_kn]:
            slacktext = f'new kilonova candidate alert: \n<http://134.158.75.151:24000/{alertID}>'
            requests.post(
                os.environ['KNWEBHOOK_MANGROVE'],
                json={'text':slacktext, 'username':'kilonova_candidates_bot'},
                headers={'Content-Type': 'application/json'},
            )
    else:
        log = logging.Logger('Kilonova filter')
        log.warning('KNWEBHOOK_MANGROVE is not defined as env variable\
        - if an alert passed the filter, message has not been sent to Slack')
    
    return f_kn