#!/usr/bin/env python3

# Copyright (C) 2016  Peter Brantsch <peter+ilias-fuse@brantsch.name>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import requests
from bs4 import BeautifulSoup
import re
import getpass
import pprint
import logging
import re
import os
import sys
import pathlib
from fusepy import FUSE, FuseOSError, Operations, LoggingMixIn
from errno import *
import stat
import argparse

from contextlib import contextmanager
import locale
import datetime
import time


@contextmanager
def setlocale(name):
    saved = locale.setlocale(locale.LC_ALL)
    try:
        yield locale.setlocale(locale.LC_ALL, name)
    finally:
        locale.setlocale(locale.LC_ALL, saved)


class Cache(object):
    class CashedFile(object):
        def __init__(self, file, data):
            self.file = file
            self.data = data
            self.time = time.time()

        def __eq__(self, other):
            return (self.file == other and type(other) == File) or (
                type(other) == type(self) and self.file == other.file)

    cache = list()

    def __init__(self, capacity, timeout):
        self.capacity = capacity
        self.cache_timeout = timeout

    def put(self, file, data):
        c = self.CashedFile(file, data)
        if c in self.cache:
            self.cache.remove(c)
        self.cache.append(c)
        while self.size() > self.capacity and len(self.cache) > 1:
            self.cache.pop(0)
            logging.info("Poped cache element.")
        logging.info("Current cache size: " + str(self.size()) + "MB")

    def get(self, file):
        if file in self.cache:
            obj = self.cache.pop(self.cache.index(file))
            if time.time() - obj.time > self.cache_timeout:
                return None
            self.cache.append(obj)
            return self.cache[len(self.cache) - 1].data
        return None

    def size(self):
        sm = 0
        for d in self.cache:
            sm += len(d.data)
        return sm / 1024.0 / 1024.0


class InvalidCredentialsError(ValueError): pass


class IliasSession(requests.Session):
    """
    Derivative of requests.Session for sessions at the ILIAS of Karlsruhe Institute of Technology.
    Attempts to log in to ILIAS via the Shibboleth Identity provider.

    Usage:
    >>> session = IliasSession(username="your_username") #next you will be prompted for your password
    >>> session.get(some_course_url) #use it just like any requests.Session instance
    """

    def __init__(self, username=None, password=None):
        super().__init__()
        self.username = username if username else input("Username: ")
        self.password = password if password else getpass.getpass()
        self.logger = logging.getLogger(type(self).__name__)
        self.login()

    def login(self):
        self.cookies.clear()
        logging.info("(Re-)logging in...")

        session_establishment_response = self.post("https://ilias.studium.kit.edu/Shibboleth.sso/Login", data={
            "sendLogin": "1",
            "idp_selection": "https://idp.scc.kit.edu/idp/shibboleth",
            "target": "https://ilias.studium.kit.edu/shib_login.php?target=",
            "home_organization_selection": "Mit KIT-Account anmelden"
        })
        jsessionid = self.cookies.get("JSESSIONID")
        login_response = self.post(session_establishment_response.url, data={
            "j_username": self.username,
            "j_password": self.password,
            "_eventId_proceed": ""
        })
        login_soup = BeautifulSoup(login_response.text, 'lxml')
        saml_response = None
        relay_state = None
        try:
            saml_response = login_soup.find("input", attrs={"name": "SAMLResponse"}).get("value")
            relay_state = login_soup.find("input", attrs={"name": "RelayState"}).get("value")
        except AttributeError as e:
            raise InvalidCredentialsError(
                "Username and/or password most likely invalid. (SAML response could not be found.)") from e
        self.post("https://ilias.studium.kit.edu/Shibboleth.sso/SAML2/POST", data={
            "SAMLResponse": saml_response,
            "RelayState": relay_state
        })


class IliasNode(object):
    @staticmethod
    def create_instance(name, url, ilias_session, html_list_item):
        """
        Call this method to get an instance of the appropriate subclass of IliasNode for the given *url*.
        """
        type_mapping = {
            re.compile(r"ilias.php\?.*cmdClass=ilrepositorygui.*"): Course,
            re.compile(r"ilias\.php\?.*cmd=view.*"): Folder,
            re.compile(r"https://ilias.studium.kit.edu/goto\.php\?.*target=file_[0-9]*_download.*"): File
        }
        first_match = next(filter(None, map(lambda r: r.match(url), type_mapping.keys())), None)
        cls = IliasNode
        if first_match:
            cls = type_mapping[first_match.re]
        return cls(name, url, ilias_session, html_list_item)

    def __repr__(self):
        return "{}(name={self.name}, url={self.url}, ilias_session={self.session})".format(type(self).__name__,
                                                                                           self=self)

    def __init__(self, name, url, ilias_session, html_list_item):
        self.session = ilias_session
        self.name = name.replace("/", "-")
        self.url = url
        self.__children = {}

    def get_children(self):
        if not self.__children:
            node_page_req = self.session.get(self.url, allow_redirects=False)
            if node_page_req.is_redirect:  # Keepalive
                self.session.login()
                node_page_req = self.session.get(self.url, allow_redirects=False)
            node_page = node_page_req.text
            soup = BeautifulSoup(node_page, 'lxml')
            child_list_items = soup.select("div.il_ContainerListItem")
            for list_item in child_list_items:
                try:
                    a = list_item.select("a.il_ContainerItemTitle")[0]
                    child_node = IliasNode.create_instance(a.text, a.get("href"), self.session, list_item)
                    logging.debug(child_node)
                    self.__children[child_node.name] = child_node
                except Exception as e:
                    logging.warn(e)
        return self.__children.values()

    def get_child_by_name(self, name):
        self.get_children()
        return self.__children.get(name)


class Course(IliasNode):
    def __init__(self, name, url, ilias_session, html_list_item):
        super().__init__(name, "https://ilias.studium.kit.edu/" + url, ilias_session, html_list_item)


class Folder(IliasNode):
    def __init__(self, name, url, ilias_session, html_list_item):
        super().__init__(name, "https://ilias.studium.kit.edu/" + url, ilias_session, html_list_item)


class File(IliasNode):
    def __init__(self, name, url, ilias_session, html_list_item):
        super().__init__(name, url, ilias_session, html_list_item)
        properties_div = html_list_item.select("div.il_ItemProperties")[0]
        properties = html_list_item.select("span.il_ItemProperty")
        ext = properties[0].text.strip()
        self.name = self.name + "." + ext.strip()
        self.size = self.human2bytes(properties[1].text)  # Approximate file size
        with setlocale('de_DE.UTF-8'):
            dtime = datetime.datetime.strptime(properties[2].text.strip(), "%d. %b %Y, %H:%M")  # TODO Timezone?
            self.time = time.mktime(dtime.timetuple())

    @staticmethod
    def human2bytes(s):
        symbols = ('B', 'KB', 'MB', 'GB', 'TB')
        rex = re.compile(r"\s*([0-9,\.]*)\s*([a-zA-Z]*)\s*", re.UNICODE)
        match = rex.match(s)
        letters = match.group(2)
        num = match.group(1).replace(",", ".")
        num = float(num)
        prefix = {symbols[0]: 1}
        for i, s in enumerate(symbols[1:]):
            prefix[s] = 1 << (i + 1) * 10
        return int(num * prefix[letters])

    def download(self, size, offset):
        file = cache.get(self)
        if file is not None:
            logging.info("Using cached file")
            return file[offset:offset + size]
        response = self.session.get(self.url, allow_redirects=False)
        if response.is_redirect:  # Ilias redirects to login form, if session expired when requesting a file...
            self.session.login()
            response = self.session.get(self.url, allow_redirects=False)  # ... so try again once.
        content = response.content
        cache.put(self, content)
        # Update size because size from overview is only approximate
        # Apparently this works
        self.size = len(content)
        return content[offset:offset + size]


class IliasDashboard(IliasNode):
    def __init__(self):
        super().__init__("Dashboard",
                         "https://ilias.studium.kit.edu/ilias.php?baseClass=ilPersonalDesktopGUI&cmd=jumpToSelectedItems",
                         IliasSession(), None)


class IliasFS(LoggingMixIn, Operations):
    def __init__(self, root, dashboard):
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.dashboard = dashboard

    def __call__(self, op, path, *args):
        return super(IliasFS, self).__call__(op, path, *args)

    def access(self, path, mode):
        if self.__path_to_object(path):
            return 0
        else:
            raise FuseOSError(ENOENT)

    def __path_to_object(self, path):
        """
        Map from paths to objects.
        """
        purepath = pathlib.PurePath(path)
        node = self.dashboard
        for part in purepath.parts[1:]:
            node = node.get_child_by_name(part)
            if not node:
                break
        return node

    def getattr(self, path, fh=None):
        node = self.__path_to_object(path)
        if node:
            is_file = (type(node) == File)
            st_mode_ft_bits = stat.S_IFREG if is_file else stat.S_IFDIR
            st_mode_permissions = 0o444 if is_file else 0o555
            return {
                'st_mode': st_mode_ft_bits | st_mode_permissions,
                'st_uid': self.uid,
                'st_gid': self.gid,
                'st_size': node.size if is_file else 0,
                'st_ctime': node.time if is_file else 0,
                'st_mtime': node.time if is_file else 0
            }
        else:
            raise FuseOSError(ENOENT)

    getxattr = None

    def readdir(self, path, fh):
        node = self.__path_to_object(path)
        return ['.', '..'] + [child.name for child in filter(lambda c: type(c) != IliasNode, node.get_children())]

    def read(self, path, size, offset, fh):
        node = self.__path_to_object(path)
        if node:
            if type(node) == File:
                val = node.download(size, offset)
                return val
            else:
                raise FuseOSError(EISDIR)
        else:
            raise FuseOSError(ENOENT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="At last, a FUSE filesystem for the ILIAS installation at KIT")
    parser.add_argument('mountpoint', type=str, help="where to mount this filesystem")
    parser.add_argument('--foreground', action='store_true', help="do not fork away to background")
    parser.add_argument('--log-level', type=str, default=logging.getLevelName(logging.INFO),
                        choices=logging._nameToLevel.keys(), help="adjust the verbosity of logging")
    parser.add_argument('--cache', type=int, default=50, help='File cache in MB')
    parser.add_argument('--cache-timeout', type=float, default=30, help='File cache timeout in minutes')
    args = parser.parse_args()

    logging.basicConfig(level=logging._nameToLevel[args.log_level])

    dashboard = IliasDashboard()
    cache = Cache(capacity=args.cache, timeout=int(args.cache_timeout * 60))
    fuse = FUSE(IliasFS(args.mountpoint, dashboard), args.mountpoint, foreground=args.foreground)
