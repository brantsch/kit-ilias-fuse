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
from fusepy.fuse import FUSE, FuseOSError, Operations, LoggingMixIn
from errno import *
import stat

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
        username = username if username else input("Username: ")
        password = password if password else getpass.getpass()
        self.logger = logging.getLogger(type(self).__name__)

        session_establishment_response = self.post("https://ilias.studium.kit.edu/Shibboleth.sso/Login", data={
            "sendLogin": "1",
            "idp_selection": "https://idp.scc.kit.edu/idp/shibboleth",
            "target": "https://ilias.studium.kit.edu/shib_login.php?target=",
            "home_organization_selection": "Mit KIT-Account anmelden"
            })
        jsessionid = self.cookies.get("JSESSIONID")
        login_response = self.post(session_establishment_response.url, data={
            "j_username": username,
            "j_password": password,
            "_eventId_proceed": ""
            })
        login_soup = BeautifulSoup(login_response.text)
        saml_response = None
        relay_state = None
        try:
            saml_response = login_soup.find("input", attrs={"name": "SAMLResponse"}).get("value")
            relay_state = login_soup.find("input", attrs={"name": "RelayState"}).get("value")
        except AttributeError as e:
            raise InvalidCredentialsError("Username and/or password most likely invalid. (SAML response could not be found.)") from e
        self.post("https://ilias.studium.kit.edu/Shibboleth.sso/SAML2/POST", data={
            "SAMLResponse": saml_response,
            "RelayState": relay_state
            })

class IliasNode():
    @staticmethod
    def create_instance(name, url, ilias_session):
        """
        Call this method to get an instance of the appropriate subclass of IliasNode for the given *url*.
        """
        type_mapping = {
                re.compile(r"https://ilias.studium.kit.edu/goto_produktiv_crs_\d+\.html"): Course,
                re.compile(r"https://ilias.studium.kit.edu/goto_produktiv_fold_\d+\.html"): Folder,
                re.compile(r"ilias.php?.*cmd=sendfile.*"): File
                }
        first_match = next(filter(None, map(lambda r: r.match(url), type_mapping.keys())), None)
        cls = IliasNode
        if first_match:
            cls = type_mapping[first_match.re]
        return cls(name, url, ilias_session)

    def __repr__(self):
        return "{}(name={self.name}, url={self.url}, ilias_session={self.session})".format(type(self).__name__, self=self)

    def __init__(self, name, url, ilias_session):
        self.session = ilias_session
        self.name = name
        self.url = url
        self.__children = {}

    def get_children(self):
        if not self.__children:
            node_page = self.session.get(self.url).text
            soup = BeautifulSoup(node_page)
            child_anchors = soup.select("a.il_ContainerItemTitle")
            for a in child_anchors:
                try:
                    child_node = IliasNode.create_instance(a.text, a.get("href"), self.session)
                    self.__children[child_node.name] = child_node
                except:
                    pass
        return self.__children.values()

    def get_child_by_name(self, name):
        self.get_children()
        return self.__children.get(name)

class Course(IliasNode):
    pass

class Folder(IliasNode):
    pass

class File(IliasNode):
    def __init__(self, name, url, ilias_session):
        """
        Due to horrible botchery both on my part and at ILIAS, the name
        attribute will be ignored and replaced by the proper full filename.
        """
        super().__init__(name, "https://ilias.studium.kit.edu/"+url, ilias_session)
        response = self.session.head(self.url)
        headers = response.headers
        self.size = int(headers['Content-Length']) #FIXME: ILIAS seems to return bogus sizes for some text files.
        self.name = headers['Content-Description']

    def download(self, size, offset):
        response = self.session.get(self.url)
        return response.content[offset:offset+size]

class IliasDashboard(IliasNode):
    def __init__(self):
        super().__init__("Dashboard", "https://ilias.studium.kit.edu/ilias.php?baseClass=ilPersonalDesktopGUI&cmd=jumpToSelectedItems", IliasSession())

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
                    'st_size': node.size if is_file else 0
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
    if len(sys.argv) != 2:
        print('usage: %s <mountpoint>' % sys.argv[0])
        exit(1)

    logging.basicConfig(level=logging.DEBUG)

    dashboard = IliasDashboard()
    fuse = FUSE(IliasFS(sys.argv[1], dashboard), sys.argv[1], foreground=True)
