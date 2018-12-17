#!/usr/bin/env python

from __future__ import print_function
from __future__ import unicode_literals

import argparse
import glob
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from six.moves.urllib.parse import urlparse

import boto3
import botocore
import yattag
from botocore.config import Config
from botocore.exceptions import ClientError


def spawn(args, capture_output=False):
    print('=>', ' '.join(args))
    if capture_output:
        return subprocess.check_output(args)
    return subprocess.check_call(args)


class Bucket(object):
    def __init__(self, url):
        if not re.match(r'^(s3:)?//', url):
            url = '//' + url
        url = urlparse(url)
        self.name = url.netloc
        self.prefix = url.path.lstrip('/')
        self.s3 = boto3.client("s3")
        self.s3_unsigned = boto3.client(
            "s3", config=Config(signature_version=botocore.UNSIGNED))
        self.region = self._get_region()

    def _get_region(self):
        # S3, for what appears to be backwards-compatibility
        # reasons, maintains a distinction between location
        # constraints and region endpoints. Newer regions have
        # equivalent regions and location constraints, so we
        # hardcode the non-equivalent regions here with hopefully no
        # automatic support future S3 regions.
        #
        # Note also that someday, Boto should handle this for us
        # instead of the AWS command line tools.
        location = self.s3.get_bucket_location(
            Bucket=self.name)["LocationConstraint"]
        if not location:
            return 'us-east-1'
        elif location == 'EU':
            return 'eu-west-1'
        else:
            return location

    def has_key(self, key):
        try:
            self.s3.head_object(Bucket=self.name, Key=key)
            return True
        except ClientError as exc:
            if exc.response['Error']['Code'] != '404':
                raise
            return False

    def generate_url(self, key):
        return self.s3_unsigned.generate_presigned_url(
            ClientMethod='get_object',
            Params={'Bucket': self.name, 'Key': key})

    def list(self, suffix=''):
        """
        Generate the keys in an S3 bucket.

        :param suffix: Only fetch keys that end with this suffix (optional).
        """
        s3 = boto3.client('s3')
        kwargs = {'Bucket': self.name}

        if self.prefix:
            kwargs['Prefix'] = self.prefix

        while True:
            resp = s3.list_objects_v2(**kwargs)
            for obj in resp['Contents']:
                key = obj['Key']
                if key.startswith(self.prefix) and key.endswith(suffix):
                    yield key

            # The S3 API is paginated, returning up to 1000 keys at a time.
            # Pass the continuation token into the next response, until we
            # reach the final page (when this field is missing).
            try:
                kwargs['ContinuationToken'] = resp['NextContinuationToken']
            except KeyError:
                break

    def sync(self, local_dir, acl):
        return spawn([
            sys.executable, '-m', 'awscli', 's3', 'sync',
            local_dir, 's3://{0}/{1}'.format(self.name, self.prefix),
            '--region', self.region, '--acl', acl])

    def put(self, body, key, acl):
        self.s3.put_object(Bucket=self.name, Body=body, ACL=acl,
                           ContentType=mimetypes.guess_type(key)[0])

    def make_index(self):
        doc, tag, text = yattag.Doc().tagtext()
        with tag('html'):
            for key in self.list('.whl'):
                with tag('a', href=self.generate_url(key)):
                    text(key)
                doc.stag('br')

        return doc.getvalue()


def build_wheels(index_url, pip_wheel_args, exclusions):
    build_dir = tempfile.mkdtemp(prefix='mkwheelhouse-')
    args = [
        sys.executable, '-m', 'pip', 'wheel',
        '--wheel-dir', build_dir,
        '--find-links', index_url,
    ] + pip_wheel_args
    spawn(args)
    for exclusion in exclusions:
        matches = glob.glob(os.path.join(build_dir, exclusion))
        for match in matches:
            os.remove(match)
    return build_dir


def parse_args():
    parser = argparse.ArgumentParser(
        description='Generate and upload wheels to an Amazon S3 wheelhouse',
        usage='mkwheelhouse [options] bucket [PACKAGE...] [pip-options]',
        epilog='Consult `pip wheel` for valid pip-options.')
    parser.add_argument('-e', '--exclude', action='append', default=[],
                        metavar='WHEEL_FILENAME',
                        help='wheels to exclude from upload')
    parser.add_argument('-a', '--acl', metavar='POLICY', default='private',
                        help='canned ACL policy to apply to uploaded objects')
    parser.add_argument('bucket',
                        help='the Amazon S3 bucket to upload wheels to')
    args, pip_wheel_args = parser.parse_known_args()
    try:
        run(args, pip_wheel_args)
    except subprocess.CalledProcessError:
        print('mkwheelhouse: detected error in subprocess, aborting!',
              file=sys.stderr)


def run(args, pip_wheel_args):
    bucket = Bucket(args.bucket)
    if not bucket.has_key('index.html'):
        bucket.put('<!DOCTYPE html><html></html>', 'index.html', acl=args.acl)
    index_url = bucket.generate_url('index.html')
    build_dir = build_wheels(index_url, pip_wheel_args, args.exclude)
    bucket.sync(build_dir, acl=args.acl)
    bucket.put(bucket.make_index(), key='index.html', acl=args.acl)
    shutil.rmtree(build_dir)
    print('mkwheelhouse: index written to', index_url)


if __name__ == '__main__':
    parse_args()
