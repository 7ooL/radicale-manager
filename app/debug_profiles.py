import sqlite3
import json
import os

db='app/profiles.db'
if not os.path.exists(db):
    print('NO_DB')
else:
    conn=sqlite3.connect(db)
    try:
        cur=conn.execute('''SELECT id,name,server_url,username,password_encrypted,enabled,last_successful_connect_at,last_error FROM connection_profiles''')
        rows=[{
            'id':r[0],'name':r[1],'server_url':r[2],'username':r[3],'password_encrypted':r[4],'enabled':bool(r[5]),'last_successful_connect_at':r[6],'last_error':r[7]
        } for r in cur.fetchall()]
        print(json.dumps(rows,indent=2))
    except Exception as e:
        print('ERROR',e)
    finally:
        conn.close()
