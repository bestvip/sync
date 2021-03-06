#!/Users/zhouyifan/.virtualenvs/work/bin/python
import hashlib
import os
import sys
import getopt
import json
import sqlite3
import requests
import qiniu
import time


class Sync(object):

    def __init__(self):
        opts, args = getopt.getopt(sys.argv[1:], 'c:')
        self.config = None
        for o, a in opts:
            if o == '-c' and os.path.exists(a):
                with open(a, 'r') as f:
                    self.config = json.load(f)

        if self.config is None:
            return

        if 'path' not in self.config:
            return

        if 'qiniu' not in self.config:
            return
        else:
            qiniu_config = self.config['qiniu']

        self.qiniu = QiniuObject(**qiniu_config)

        if not os.path.exists(self.config['path']):
            os.mkdir(self.config['path'])

        os.chdir(self.config['path'])
        db_path = '.md5.sqlite'
        if os.path.exists(db_path):
            info = self.qiniu.get_file_info(db_path)
            if info:
                ret = json.loads(info.text_body)
                if os.stat(db_path).st_atime < ret['putTime'] / 10000000:
                    self.qiniu.download_and_save_file(db_path)
        else:
            self.qiniu.download_and_save_file(db_path)

        self.db = DBObject(db_path)
        self._update_db()

    def _update_db(self):
        def update_file(sub_path, md5):
            row = self.db.get(sub_path)
            if row:
                db_md5 = row['md5']
                if db_md5 != md5:
                    mtime = os.stat(sub_path).st_mtime
                    db_time = time.mktime(time.strptime(row['updated_at'], '%Y-%m-%d %H:%M:%S'))
                    if mtime > db_time:
                        self.qiniu.upload_file(sub_path)
                        self.db.save(sub_path, md5)
                        print('{md5} {sub_path}'.format(md5=md5, sub_path=sub_path))
                    elif mtime < db_time:
                        self.qiniu.download_and_save_file(sub_path)
                        self.db.save(sub_path, md5sum(sub_path))
                        print('{md5} {sub_path}'.format(md5=md5, sub_path=sub_path))
            else:
                info = self.qiniu.upload_file(sub_path)
                # todo: save put_time
                self.db.save(sub_path, md5)
                print('{md5} {sub_path}'.format(md5=md5, sub_path=sub_path))

        self._travel_path('.', update_file)
        self.qiniu.upload_file('.md5.sqlite')

        for row in self.db.get_all():
            path = row['full_path']
            if os.path.exists(path):
                continue

            self.qiniu.download_and_save_file(path)

    def _travel_path(self, path, callback):
        if not os.path.exists(path):
            return

        for sub_path in os.listdir(path):
            if path != '.':
                sub_path = path + '/' + sub_path
            else:
                sub_path = sub_path
            if os.path.isdir(sub_path):
                self._travel_path(sub_path, callback)
            elif os.path.isfile(sub_path):
                callback(sub_path, md5sum(sub_path))


def md5sum(file_name):
    def read_chunks(fp):
        fp.seek(0)
        chunk = fp.read(8 * 1024)
        while chunk:
            yield chunk
            chunk = fp.read(8 * 1024)
        else:
            fp.seek(0)

    m = hashlib.md5()

    if not os.path.exists(file_name):
        return None

    with open(file_name, 'rb') as fp:
        for chunk in read_chunks(fp):
            m.update(chunk)

    return m.hexdigest()


class QiniuObject(object):

    def __init__(self, access_key, secret_key, bucket_name, bucket_domain):
        self.q = qiniu.Auth(access_key, secret_key)
        self.bucket_name = bucket_name
        self.bucket_domain = bucket_domain

    def upload_file(self, path):
        token = self.q.upload_token(self.bucket_name, path, 3600)
        ret, info = qiniu.put_file(token, path, path)
        return info

    def download_file(self, path):
        base_url = 'http://%s/%s' % (self.bucket_domain, path)

        private_url = self.q.private_download_url(base_url, expires=3600)
        r = requests.get(private_url)
        if 200 <= r.status_code < 300:
            return r
        return None

    def download_and_save_file(self, path):
        r = self.download_file(path)
        if r is None:
            return

        basename, filename = os.path.split(path)
        if basename and not os.path.exists(basename):
            os.mkdir(basename)

        with open(path, 'wb') as f:
            f.write(r.content)

    def delete_file(self, path):
        bucket = qiniu.BucketManager(self.q)
        ret, info = bucket.delete(self.bucket_name, path)
        return info

    def get_file_info(self, path):
        bucket = qiniu.BucketManager(self.q)
        ret, info = bucket.stat(self.bucket_name, path)
        if ret is None:
            return None
        return info


class DBObject(object):

    def __init__(self, db_path):
        self.db_path = db_path
        if not os.path.exists(self.db_path):
            self._init_db()
        else:
            self.conn = sqlite3.connect(self.db_path)

        self.conn.row_factory = sqlite3.Row

    def _init_db(self):
        self.conn = sqlite3.connect(self.db_path)
        cur = self.conn.cursor()
        cur.execute('''
            CREATE TABLE file_md5 (
              full_path  TEXT PRIMARY KEY,
              md5        TEXT,
              created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
              updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )''')
        self.conn.commit()

    def save(self, full_path, md5):
        cur = self.conn.cursor()

        def update(full_path, md5):
            cur.execute('''
            UPDATE file_md5
            SET full_path=?,
                md5=?,
                updated_at=(datetime('now', 'localtime'))
            WHERE full_path=?
            ''', (full_path, md5, full_path))

        def insert(full_path, md5):
            cur.execute('''
            INSERT INTO file_md5(full_path, md5)
            VALUES(?, ?)
            ''', (full_path, md5))

        if self.get(full_path):
            update(full_path, md5)
        else:
            insert(full_path, md5)

        self.conn.commit()

    def get(self, full_path):
        cur = self.conn.cursor()
        cur.execute('''
        SELECT *
        FROM file_md5
        WHERE full_path=?
        ''', (full_path,))

        return cur.fetchone()

    def get_all(self):
        cur = self.conn.cursor()
        cur.execute('''
        SELECT *
        FROM file_md5
        ''')

        return cur.fetchall()

    def delete(self, full_path):
        cur = self.conn.cursor()
        cur.execute('''
        DELETE FROM file_md5 WHERE full_path=?
        ''', (full_path,))
        self.conn.commit()


if __name__ == '__main__':
    sync = Sync()
