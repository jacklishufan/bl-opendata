import functools, os, sys
import numpy as np
import tempfile
import matplotlib.pyplot as plt
from flask import (
    Blueprint, flash, g, redirect, render_template, request, session, url_for, send_from_directory, jsonify, abort, current_app
)
from werkzeug.utils import secure_filename
from astropy import units as u
from astropy.time import Time
from astropy.coordinates import SkyCoord
from astroquery.simbad import Simbad
import pickle
from . import mysql

DATA_FOLDER = 'data'
PUBLIC_FOLDER = '../data'
TMP_FOLDER = 'tmp'
SYS_TMP_FOLDER = tempfile.gettempdir()

STATIC_FOLDER = os.path.join('blopendata', 'static')
SIMBAD_CACHE_PATH = os.path.join(DATA_FOLDER, 'simbad-ids.pkl')

bp = Blueprint('core', __name__, url_prefix='/')

simbad_cache = {}
def _query_simbad(target):
    """ query SIMBAD database for synonyms for 'target' """
    global simbad_cache
    target = target.strip();
    if target not in simbad_cache.keys():
        ids = None
        clean_exts = ['_OFF', '_OFFA', '_OFFB']
        ids = Simbad.query_objectids(target)
        if ids is not None:
            ids_lst = []
            for id in ids:
                spl = id[0].split()
                if spl[0] == '*' or spl[0] == '**' or spl[0] == 'V*':
                    spl[0] = "" # cut off *, **, V*
                if spl[0] == "NAME":
                    # put at beginning
                    ids_lst.append(ids_lst[0] if len(ids_lst) > 0 else 0)
                    ids_lst[0] = ' '.join(spl[1:])
                else:
                    # cut off space, unless first word is surrounded by []
                    if len(spl[0]) == 0 or spl[0][-1] != ']':
                        ids_lst.append(spl[0] + ' '.join(spl[1:]))
                    else:
                        # do not remove first space
                        ids_lst.append(' '.join(spl))
            simbad_cache[target] = ids_lst
            if target not in ids_lst:
                ids_lst.append(target)
        else:
            simbad_cache[target] = [target]
    return simbad_cache[target]

# cache SIMBAD
if os.path.exists(SIMBAD_CACHE_PATH):
    simbad_cache = pickle.load(open(SIMBAD_CACHE_PATH, 'rb'))
conn = mysql.connect()
cursor = conn.cursor()
cursor.execute('SELECT DISTINCT target_name FROM files')
targets = [x[0] for x in cursor.fetchall()]
conn.close()
print("Rerunning SIMBAD ID queries...")
simbad_reran = 0
for target in targets:
    if target not in simbad_cache.keys():
        simbad_reran += 1
    _query_simbad(target)
print("Done with SIMBAD queries, reran a total of", simbad_reran, "queries")
pickle.dump(simbad_cache, open(SIMBAD_CACHE_PATH, 'wb'))

@bp.route('/', methods=('GET', 'POST'))
def home():
    # home page
    return render_template('core/home.html', wat_image=request.args.get('wat_image'))

@bp.route('/data/<path:filename>', methods=('GET',))
def serve_public(filename):
    # serve a public file
    return send_from_directory(PUBLIC_FOLDER, filename)
    
# API
@bp.route('/api/list-targets', methods=('GET', 'POST'))
def api_list_targets():
    """ get list of all targets in the database at this time """
    conn = mysql.connect()
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT target_name FROM files')
    targets = [x[0] for x in cursor.fetchall()]
    conn.close()
    if 'simbad' in request.args:
        target_dict = {}
        for target in targets:
            target_dict[target] = _query_simbad(target)
        return jsonify(target_dict)
    else:
        return jsonify(targets)
    
@bp.route('/api/list-telescopes', methods=('GET', 'POST'))
def api_list_telescopes():
    """ get list of all telescopes in the database at this time """
    conn = mysql.connect()
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT project FROM files')
    projects = cursor.fetchall()
    conn.close()
    return jsonify(projects)
    
@bp.route('/api/list-file-types', methods=('GET', 'POST'))
def api_list_file_types():
    """ get list of all file type names in the database at this time """
    conn = mysql.connect()
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT file_type FROM files')
    ftypes = cursor.fetchall()
    conn.close()
    return jsonify(ftypes)
    
@bp.route('/api/query-files', methods=('GET', 'POST'))
def api_query():
    """ 
    query for a list of files with associated info 
    possible arguments: target, telescopes (comma-sep), file-types (comma-sep), 
                        pos-ra, pos-dec, pos-rad, time-start, time-end, freq-start, freq-end, limit
    returns: dictionary d with d["result"] in {"success", "error"}
                               d["message"] set to message on result "error"
                               d["data"] set to query result set on result "success"
                                         is a list of dictionaries, each with keys
                                         ['target', 'telescope', 'utc', 'mjd', 'ra',
                                          'decl', 'center_freq', 'file_type', 'size', 'md5sum', 'url']
    """
    # get args
    if 'target' not in request.args:
        return jsonify({'result': 'error', 'message': 'Target is required'})
        
    target = request.args.get('target')
    
    sql_cmd = 'SELECT * FROM files WHERE target_name'
    sql_args = []

    if len(target) > 1 and target[0] in ['!', '/']:
        if target[0] == '!':
            sql_cmd += ' = %s'
        elif target[0] == '/':
            sql_cmd += ' REGEXP %s'
        sql_args.append(target[1:])
    else:
        sql_cmd += ' LIKE %s'
        sql_args.append('%' + target + ('%' if target else ''))
    
    if 'telescopes' in request.args:
        telescopes_str = request.args.get('telescopes').split(',')
        sql_cmd += " AND project IN ({})".format(",".join(["%s"] * len(telescopes_str)))
        sql_args.extend(telescopes_str)
        
    if 'file-types' in request.args:
        ftypes_str = request.args.get('file-types').split(',')
        if 'fits' in ftypes_str:
            # data is synonym for fits
            ftypes_str.append('data')
        sql_cmd += " AND file_type IN ({})".format(",".join(["%s"] * len(ftypes_str)))
        sql_args.extend(ftypes_str)
        
    cen_coord, rad = None, -1.
    if 'pos-ra' in request.args and 'pos-dec' in request.args and 'pos-rad' in request.args:
        ra = float(request.args.get('pos-ra'))
        decl = float(request.args.get('pos-dec'))
        rad = float(request.args.get('pos-rad'))
        # try to limit the range of queried ra, decl based on the required position/radius
        decl_min, decl_max = max(decl - rad, -90.), min(decl + rad, 90.)
        from math import cos
        ra_per_dec = abs(cos(decl))
        ra_min, ra_max = ra - ra_per_dec * rad, ra + ra_per_dec * rad
        sql_cmd += " AND decl BETWEEN %s AND %s"
        sql_args.extend([decl_min, decl_max])
        if ra_max - ra_min >= 360.:
            pass # can't filter
        elif ra_max > 360. or ra_min < 0:
            ra_min = (ra_min + 360.) % 360.
            ra_max = (ra_max + 360.) % 360.
            if ra_min < ra_max:
                sql_cmd += " AND ra NOT BETWEEN %s AND %s"
                sql_args.extend([ra_min, ra_max])
        else:
            sql_cmd += " AND ra BETWEEN %s AND %s"
            sql_args.extend([ra_min, ra_max])
        cen_coord = SkyCoord(ra = ra * u.deg, dec = decl * u.deg)
        
    if 'time-start' in request.args:
        t_start = Time(float(request.args.get('time-start')), format='mjd')
        sql_cmd +=  " AND utc_observed >= %s"
        sql_args.append(str(t_start.utc.iso))
        
    if 'time-end' in request.args:
        t_end = Time(float(request.args.get('time-end')), format='mjd')
        sql_cmd +=  " AND utc_observed <= %s"
        sql_args.append(str(t_end.utc.iso))
        
    if 'freq-start' in request.args:
        f_start = float(request.args.get('freq-start'))
        sql_cmd +=  " AND center_freq >= %s"
        sql_args.append(f_start)
        
    if 'freq-end' in request.args:
        f_end = float(request.args.get('freq-end'))
        sql_cmd +=  " AND center_freq <= %s"
        sql_args.append(f_end)
        
    if 'limit' in request.args:
        lim = int(request.args.get('limit'))
        sql_cmd +=  " LIMIT %s"
        sql_args.append(lim)
    
    conn = mysql.connect()
    cursor = conn.cursor()
    cursor.execute(sql_cmd, sql_args)
    table = cursor.fetchall()
    conn.close()
    
    data = []
    for row in table:
        entry = {}
        entry['target'] = row[3]
        entry['telescope'] = row[1]
        entry['utc'] = row[2]
        entry['mjd'] = Time(str(row[2]), format='iso').mjd
        entry['ra'] = row[4]
        entry['decl'] = row[5]
        entry['center_freq'] = row[6]
        entry['file_type'] = row[7]
        entry['size'] = row[8]
        entry['md5sum'] = row[9]
        entry['url'] = row[10]
        if cen_coord is not None:
            coord = SkyCoord(ra = entry['ra'] * u.deg, dec = entry['decl'] * u.deg)
            #print(coord, file=sys.stderr)
            if cen_coord.separation(coord) > rad * u.deg:
                # out of position query range
                continue
        data.append(entry)
    return jsonify({'result': 'success', 'data': data})
