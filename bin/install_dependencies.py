#!/usr/bin/env python
"""
This script installs the external dependencies for InterTwinkles, along with
the node modules, including: solr, etherpad-lite, and everything listed in
package.json.
"""
import os
import re
import sys
import json
import time
import shutil
import base64
import tarfile
import urllib2
import tempfile
import argparse
import subprocess

SOLR_VERSION = "4.1.0"
SOLR_INSTALLER = "http://apache.mirrors.pair.com/lucene/solr/{0}/solr-{0}.tgz".format(SOLR_VERSION)
ETHERPAD_REPOSITORY = "https://github.com/ether/etherpad-lite.git"
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
VENDOR_DIR = os.path.join(PROJECT_ROOT, "vendor")
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
SECRETS_DIR = os.path.join(CONFIG_DIR, "secrets")
SECRET_LENGTH = 64

parser = argparse.ArgumentParser(
        description="Install prerequisites for InterTwinkles.")
parser.add_argument("include", metavar='APP', nargs='*',
        help="List tasks to complete: ['node', 'secrets', 'solr', 'etherpad']")

def install_all():
    args = parser.parse_args()
    include = set(args.include or ["node", "secrets", "solr", "etherpad"])
    if "secrets" in include:
        print("Creating secrets")
        create_secrets()
    if "node" in include:
        print("Installing node dependencies")
        install_node_dependencies()
    if "solr" in include:
        print("Installing solr")
        install_solr()
    if "etherpad" in include:
        print("Installing etherpad")
        install_etherpad()

def create_secrets():
    if not os.path.exists(SECRETS_DIR):
        os.makedirs(SECRETS_DIR)
    for fname in ("API_KEY.txt", "SECRET.txt"):
        key_path = os.path.join(SECRETS_DIR, fname)
        if not os.path.exists(key_path):
            with open(key_path, 'w') as fh:
                secret = base64.urlsafe_b64encode(os.urandom(SECRET_LENGTH))
                # base64 encoding makes it bigger; chomp it down.
                secret = secret[0:SECRET_LENGTH]
                fh.write(secret)

def install_node_dependencies():
    subprocess.check_call(["npm", "install"], cwd=PROJECT_ROOT)

def install_solr():
    dest = os.path.join(VENDOR_DIR, "solr")
    try:
        # Prepare destination directory.
        if not os.path.exists(dest):
            os.makedirs(dest)

        # Download and extract solr.
        solr = os.path.join(dest, "solr-{0}".format(SOLR_VERSION))
        if not os.path.exists(solr):
            response = urllib2.urlopen(SOLR_INSTALLER)
            with tempfile.NamedTemporaryFile(suffix=".tgz") as fh:
                fh.write(response.read())
                fh.flush()
                os.fsync(fh.fileno())
                with tarfile.open(fh.name) as tar:
                    tar.extractall(dest)

        # Copy schema and logging properties
        _overwrite_link(
            os.path.join(CONFIG_DIR, "solr", "schema.xml"),
            os.path.join(
                solr, "example", "solr", "collection1", "conf", "schema.xml"))
        _overwrite_link(
            os.path.join(CONFIG_DIR, "solr", "logging.properties"),
            os.path.join(solr, "example", "logging.properties"))

        # Create start script
        start_script = os.path.join(dest, "start.sh")
        if not os.path.exists(start_script):
            with open(start_script, 'w') as fh:
                fh.write("""#!/bin/bash
cd ${{0%/*}}/solr-{0}/example/
java -Djava.util.logging.config.file=logging.properties -jar -server start.jar
""".format(SOLR_VERSION))
            os.chmod(start_script, 0755)

    except Exception:
        shutil.rmtree(dest)
        raise

def install_etherpad():
    dest = os.path.join(VENDOR_DIR, "etherpad-lite")
    # Clone etherpad
    if not os.path.exists(dest):
        subprocess.check_call(["git", "clone", ETHERPAD_REPOSITORY],
                cwd=VENDOR_DIR)
    else:
        subprocess.call(["git", "pull", "origin", "master"], cwd=dest)

    # Install settings
    _overwrite_link(
        os.path.join(CONFIG_DIR, "etherpad", "settings.json"),
        os.path.join(dest, "settings.json"))
    _overwrite_link(
        os.path.join(CONFIG_DIR, "etherpad", "pad.css"),
        os.path.join(dest, "src", "static", "custom", "pad.css"))

    # Etherpad has an "installDeps.sh" script which installs some dependencies.
    # However, not everything is created or initialized until you first run the
    # server and hit it with a page request.  We want to run etherpad under
    # users that don't have permissions to do this initialization, so we must
    # launch and access etherpad first here to do the initialization.

    # Install dependencies...
    subprocess.check_call([os.path.abspath(
        os.path.join(dest, "bin", "installDeps.sh"))],
        cwd=os.path.join(dest, "bin"))

    # Install plugins
    subprocess.check_call(["npm", "install", "ep_headings"], cwd=dest)

    # Run the etherpad server.
    proc = subprocess.Popen(["node",
        os.path.join("node_modules", "ep_etherpad-lite", "node", "server.js")
    ], cwd=dest)
    # Wait for server to start.
    time.sleep(2)

    # Get the URL to the server from etherpad's json config. This is
    # complicated by etherpad's choice to extend the json to include comments;
    # so we must remove those before parsing the json.
    with open(os.path.join(dest, "settings.json")) as fh:
        content = comment_remover(fh.read()) 
        try:
            settings = json.loads(content)
        except ValueError:
            print "Error parsing JSON-with-comments. Stripped JSON:"
            print content
            proc.kill()
            raise ValueError(
                    "Couldn't parse JSON-with-comments in '{0}'".format(
                        os.path.join(dest, "settings.json")
                    )
            )
        ep_ip = settings.get("ip", "0.0.0.0")
        ep_port = settings.get("port", 9001)

    # Access the server to trigger final initialization.
    print "Accessinpg etherpad to install dependencies..."
    req = urllib2.urlopen("http://{0}:{1}".format(ep_ip, ep_port))
    req.read()
    status = req.getcode()
    print "Status", status
    proc.kill()
    assert status == 200

def _overwrite_link(source, dest):
    try:
        os.remove(dest)
    except OSError:
        pass
    os.symlink(os.path.relpath(source, os.path.dirname(dest)), dest)

def comment_remover(text):
    def replacer(match):
        s = match.group(0)
        if s.startswith('/'):
            return ""
        else:
            return s
    pattern = re.compile(
        r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
        re.DOTALL | re.MULTILINE
    )
    return re.sub(pattern, replacer, text)

if __name__ == "__main__":
    install_all()
