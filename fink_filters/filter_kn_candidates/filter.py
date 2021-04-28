# Copyright 2019-2021 AstroLab Software
# Authors: Julien Peloton, Juliette Vlieghe
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
import requests
import os
import logging

from astropy.coordinates import SkyCoord
from astropy.coordinates import Angle
from astropy import units as u
from astropy.time import Time

from fink_science.conversion import dc_mag


@pandas_udf(BooleanType(), PandasUDFType.SCALAR)
def kn_candidates(
        objectId, knscore, rfscore, snn_snia_vs_nonia, snn_sn_vs_all, drb,
        classtar, jdstarthist, ndethist, cdsxmatch, ra, dec, cjd, cfid,
        cmagpsf, csigmapsf, cmagnr, csigmagnr, cmagzpsci, cisdiffpos
        ) -> pd.Series:
    """ Return alerts considered as KN candidates.
    If the environment variable KNWEBHOOK is defined and match a webhook url,
    the alerts that pass the filter will be sent to the matching Slack channel.

    Parameters
    ----------
    objectId: Spark DataFrame Column
        Column containing the alert IDs
    knscore, rfscore, snn_snia_vs_nonia, snn_sn_vs_all: Spark DataFrame Columns
        Columns containing the scores for: 'Kilonova', 'Early SN Ia',
        'Ia SN vs non-Ia SN', 'SN Ia and Core-Collapse vs non-SN events'
    drb: Spark DataFrame Column
        Column containing the Deep-Learning Real Bogus score
    classtar: Spark DataFrame Column
        Column containing the sextractor score
    jdstarthist: Spark DataFrame Column
        Column containing earliest Julian dates of epoch [days]
    ndethist: Spark DataFrame Column
        Column containing the number of prior detections (theshold of 3 sigma)
    cdsxmatch: Spark DataFrame Column
        Column containing the cross-match values
    ra: Spark DataFrame Column
        Column containing the right Ascension of candidate; J2000 [deg]
    dec: Spark DataFrame Column
        Column containing the declination of candidate; J2000 [deg]
    cjd, cfid, cmagpsf, csigmapsf, cmagnr, csigmagnr, cmagzpsci: Spark DataFrame Columns
        Columns containing history of fid, magpsf, sigmapsf, magnr, sigmagnr,
        magzpsci, isdiffpos as arrays
    Returns
    ----------
    out: pandas.Series of bool
        Return a Pandas DataFrame with the appropriate flag:
        false for bad alert, and true for good alert.
    """
    # Extract last (new) measurement from the concatenated column
    jd = cjd.apply(lambda x: x[-1])
    fid = cfid.apply(lambda x: x[-1])

    high_knscore = knscore.astype(float) > 0.5
    high_drb = drb.astype(float) > 0.5
    high_classtar = classtar.astype(float) > 0.4
    new_detection = jd.astype(float) - jdstarthist.astype(float) < 20
    small_detection_history = ndethist.astype(float) < 20

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
        ["Unknown", "Transient", "Fail"] + list_simbad_galaxies

    f_kn = high_knscore & high_drb & high_classtar & new_detection
    f_kn = f_kn & small_detection_history & cdsxmatch.isin(keep_cds)

    if 'KNWEBHOOK' in os.environ:
        if f_kn.any():
            # Galactic latitude transformation
            b = SkyCoord(
                np.array(ra[f_kn], dtype=float),
                np.array(dec[f_kn], dtype=float),
                unit='deg'
            ).galactic.b.deg

            # Simplify notations
            ra = Angle(
                np.array(ra.astype(float)[f_kn]) * u.degree
            ).deg
            dec = Angle(
                np.array(dec.astype(float)[f_kn]) * u.degree
            ).deg
            ra_formatted = Angle(ra*u.degree).to_string(precision=2, sep=' ',
                                                        unit=u.hour)
            dec_formatted = Angle(dec*u.degree).to_string(precision=1, sep=' ',
                                                         alwayssign=True)
            delta_jd_first = np.array(
                jd.astype(float)[f_kn] - jdstarthist.astype(float)[f_kn]
            )
            knscore = np.array(knscore.astype(float)[f_kn])
            rfscore = np.array(rfscore.astype(float)[f_kn])
            snn_snia_vs_nonia = np.array(snn_snia_vs_nonia.astype(float)[f_kn])
            snn_sn_vs_all = np.array(snn_sn_vs_all.astype(float)[f_kn])

            # Redefine jd & fid relative to candidates
            fid = np.array(fid.astype(int)[f_kn])
            jd = np.array(jd)[f_kn]

        dict_filt = {1: 'g', 2: 'r'}
        for i, alertID in enumerate(objectId[f_kn]):
            # Careful - Spark casts None as NaN!
            maskNotNone = ~np.isnan(np.array(cmagpsf[f_kn].values[i]))

            # Initialise containers
            rate = {1: float('nan'), 2: float('nan')}
            mag = {1: float('nan'), 2: float('nan')}
            err_mag = {1: float('nan'), 2: float('nan')}

            # Time since last detection (independently of the band)
            jd_hist_allbands = np.array(np.array(cjd[f_kn])[i])[maskNotNone]
            delta_jd_last = jd_hist_allbands[-1] - jd_hist_allbands[-2]

            # This could be further simplified as we only care
            # about the filter of the last measurement.
            # But the loop is fast enough to keep it for the moment
            # (and it could be  useful later to have a
            # general way to extract rates etc.)
            for filt in [1, 2]:
                maskFilter = np.array(cfid[f_kn].values[i]) == filt
                m = maskNotNone * maskFilter

                # DC mag (history + last measurement)
                mag_hist, err_hist = np.array([
                    dc_mag(k[0], k[1], k[2], k[3], k[4], k[5], k[6])
                    for k in zip(
                        cfid[f_kn].values[i][m],
                        cmagpsf[f_kn].values[i][m],
                        csigmapsf[f_kn].values[i][m],
                        cmagnr[f_kn].values[i][m],
                        csigmagnr[f_kn].values[i][m],
                        cmagzpsci[f_kn].values[i][m],
                        cisdiffpos[f_kn].values[i][m]
                    )
                ]).T

                # Grab the last measurement and its error estimate
                mag[filt] = mag_hist[-1]
                err_mag[filt] = err_hist[-1]

                # Compute rate only if more than 1 measurement available
                if len(mag_hist) > 1:
                    jd_hist = cjd[f_kn].values[i][m]

                    # rate is between `last` and `last-1` measurements only
                    dmag = mag_hist[-1] - mag_hist[-2]
                    dt = jd_hist[-1] - jd_hist[-2]
                    rate[filt] = dmag / dt

            # information to send
            alert_text = """
                *New kilonova candidate:* <http://134.158.75.151:24000/{}|{}>
                """.format(alertID, alertID)
            knscore_text = "*Kilonova score:* {:.2f}".format(knscore[i])
            score_text = """
                *Other scores:*\n- Early SN Ia: {:.2f}\n- Ia SN vs non-Ia SN: {:.2f}\n- SN Ia and Core-Collapse vs non-SN: {:.2f}
                """.format(rfscore[i], snn_snia_vs_nonia[i], snn_sn_vs_all[i])
            time_text = """
                *Time:*\n- {} UTC\n - Time since last detection: {:.1f} days\n - Time since first detection: {:.1f} days
                """.format(Time(jd[i], format='jd').iso, delta_jd_last, delta_jd_first[i])
            measurements_text = """
                *Measurement (band {}):*\n- Apparent magnitude: {:.2f} ± {:.2f} \n- Rate: {:.2f} mag/day\n
                """.format(dict_filt[fid[i]], mag[fid[i]], err_mag[fid[i]], rate[fid[i]])
            radec_text = """
                 *RA/Dec:*\n- [hours, deg]: {} {}\n- [deg, deg]: {:.7f} {:+.7f}
                 """.format(ra_formatted[i], dec_formatted[i], ra[i], dec[i])
            galactic_position_text = """
                *Galactic latitude:*\n- [deg]: {:.7f}""".format(b[i])

            tns_text = '*TNS:* <https://www.wis-tns.org/search?ra={}&decl={}&radius=5&coords_unit=arcsec|link>'.format(ra[i], dec[i])
            # message formatting
            blocks = [
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": alert_text
                        },
                        {
                            "type": "mrkdwn",
                            "text": knscore_text
                        }
                    ]
                 },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": time_text
                        },
                        {
                            "type": "mrkdwn",
                            "text": score_text
                        },
                        {
                            "type": "mrkdwn",
                            "text": radec_text
                        },
                        {
                            "type": "mrkdwn",
                            "text": measurements_text
                        },
                        {
                            "type": "mrkdwn",
                            "text": galactic_position_text
                        },
                        {
                            "type": "mrkdwn",
                            "text": tns_text
                        },
                    ]
                },
            ]
            requests.post(
                os.environ['KNWEBHOOK'],
                json={
                    'blocks': blocks,
                    'username': 'Classifier-based kilonova bot'
                },
                headers={'Content-Type': 'application/json'},
            )
    else:
        log = logging.Logger('Kilonova filter')
        msg = """
        KNWEBHOOK is not defined as env variable
        if an alert has passed the filter,
        the message has not been sent to Slack
        """
        log.warning(msg)

    return f_kn