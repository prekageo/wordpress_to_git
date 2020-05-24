#!/usr/bin/env python3

import getpass
import git
import gitdb
import io
import json
import logging
import os
import requests
import time
import urllib

class Site:
    def __init__(self, ID, URL, **kwargs):
        self.id = ID
        self.url = URL

class Post:
    def __init__(self, site, type, ID, slug, title, content, date, **kwargs):
        self.site = site
        self.type = type
        self.id = ID
        self.slug = urllib.parse.unquote(slug)
        self.title = title
        self.content = content
        self.date = date.replace('+00:00', '')
        self.revision_ids = kwargs.get('revisions', [])
        self.attachments = []

class Attachment:
    def __init__(self, ID, URL, data, **kwargs):
        self.id = ID
        self.url = URL
        self.data = data

class PostRevision:
    def __init__(self, post, id, post_title, post_content, post_modified_gmt, **kwargs):
        self.post = post
        self.id = id
        self.post_title = post_title
        self.post_content = post_content
        self.post_modified_gmt = post_modified_gmt.replace('Z', '')

class WordPress:
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; Win64; x64; rv:76.0) Gecko/20100101 Firefox/76.0',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        }

    def get(self, url, key=None):
        cache_filename = f'tmp_{key}.html'
        if not os.path.exists(cache_filename):
            r = self._get(url)
            with open(cache_filename, 'wb') as f:
                f.write(r.content)
            return r.content
        else:
            with open(cache_filename, 'rb') as f:
                return f.read()

    def _get(self, url):
        logging.debug('%s %s', 'GET', url)
        time.sleep(30)
        headers = {
            **self.headers,
            'Authorization': 'X-WPCOOKIE ' + self.session.cookies['wp_api'] + ':1:https://wordpress.com',
            'Referer': 'https://public-api.wordpress.com/wp-admin/rest-proxy/?v=2.0',
        }
        r = self.session.get(url, headers=headers)
        return r

    def login(self, username, password):
        data = {
            'username': username,
            'password': password,
            'remember_me': 'false',
            'redirect_to': 'https://wordpress.com/',
            'client_id': 39911,
            'client_secret': 'cOaYKdrkgXz8xY7aysv4fU6wL6sK5J8a6ojReEIAPwggsznj4Cb6mW0nffTxtYT8',
            'domain': '',
        }
        headers = {
            **self.headers,
            'Referer': 'https://wordpress.com/',
        }
        r = self.session.post('https://wordpress.com/wp-login.php?action=login-endpoint', headers=headers, data=data)
        response = json.loads(r.content)
        assert response['success'] == True

        self.session.get('https://public-api.wordpress.com/wp-admin/rest-proxy/?v=2.0', headers=headers)

    def get_sites(self):
        url = 'https://public-api.wordpress.com/rest/v1.2/me/sites?http_envelope=1&site_visibility=all&include_domain_only=true&site_activity=active'
        key = 'sites'
        data = json.loads(self.get(url, key))
        for site in data['body']['sites']:
            yield Site(**site)

    def get_posts(self, site, type):
        page = 1
        count = 0
        while True:
            url = f'https://public-api.wordpress.com/rest/v1.1/sites/{site.id}/posts?http_envelope=1&author=&number=20&order=DESC&search=&status=publish%2Cprivate&type={type}&page={page}'
            key = f'posts_{site.id}_{type}_{page}'
            data = json.loads(self.get(url, key))
            for post in data['body']['posts']:
                yield self.get_post(site, post['ID'])
                count += 1
            if count >= data['body']['found']:
                break
            page += 1

    def get_post(self, site, post_id):
        url = f'https://public-api.wordpress.com/rest/v1.1/sites/{site.id}/posts/{post_id}?http_envelope=1&context=edit&meta=autosave'
        key = f'post_{site.id}_{post_id}'
        data = json.loads(self.get(url, key))
        post_data = data['body']
        post = Post(site=site, **post_data)
        for attachment in post_data['attachments'].values():
            post.attachments.append(self.get_attachment(site, attachment))
        return post

    def get_attachment(self, site, attachment_data):
        id = attachment_data['ID']
        key = f'attachment_{site.id}_{id}'
        data = self.get(attachment_data['URL'], key)
        return Attachment(data=data, **attachment_data)

    def get_post_revisions(self, post):
        if len(post.revision_ids) == 0:
            yield PostRevision(post=post, id=post.id, post_title=post.title, post_content=post.content, post_modified_gmt=post.date)
            return
        url = f'https://public-api.wordpress.com/rest/v1.2/sites/{post.site.id}/post/{post.id}/diffs?http_envelope=1'
        key = f'post_history_{post.site.id}_{post.id}'
        data = json.loads(self.get(url, key))
        revisions = data['body']['revisions']
        for revision in revisions.values():
            yield PostRevision(post=post, **revision)

def git_add(repo, path, data):
    istream = repo.odb.store(gitdb.IStream(git.Blob.type, len(data), io.BytesIO(data)))
    entry = git.BaseIndexEntry((git.index.fun.stat_mode_to_index_mode(0o644), istream.binsha, 0, path))
    repo.index.add([entry])

def main():
    logging.basicConfig(level=logging.DEBUG)

    assert not os.path.exists('repo')

    wordpress = WordPress()

    username = input('Username: ')
    password = getpass.getpass()
    wordpress.login(username, password)

    revisions = []
    for site in wordpress.get_sites():
        for type in ['page', 'post']:
            for post in wordpress.get_posts(site, type):
                for revision in wordpress.get_post_revisions(post):
                    revisions.append(revision)

    revisions.sort(key=lambda r: r.post_modified_gmt)
    post_seen = set()

    repo = git.Repo.init('repo')

    for revision in revisions:
        domain = urllib.parse.urlparse(revision.post.site.url).netloc
        site_path = f'{revision.post.site.id}_{domain}'
        path = f'{site_path}/{revision.post.type}/{revision.post.id}_{revision.post.slug}.html'
        data = revision.post_content.encode()
        git_add(repo, path, data)

        if revision.post not in post_seen:
            post_seen.add(revision.post)
            for attachment in revision.post.attachments:
                path = f'{site_path}/files' + urllib.parse.urlparse(attachment.url).path
                git_add(repo, path, attachment.data)

        commit_msg = f'{revision.post.id}: {revision.post_title}\n'
        repo.index.commit(commit_msg, author_date=revision.post_modified_gmt, commit_date=revision.post_modified_gmt)

if __name__ == '__main__':
    main()
