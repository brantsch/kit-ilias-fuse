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
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
from errno import *
import stat
import argparse

from contextlib import contextmanager, suppress
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


def get_user_pass(from_keyring=True, ask=True):
    """get the username and password. If saved=True, try to fetch them from the
    system keyring. If ask=True, ask for the password if it is not given"""

    username, password = None, None

    if from_keyring:
        try:
            import keyring
            # Dirty hack: this uses the keyring to save the username as a
            # password.  This means that we don't need to have any extra config
            # file. It also means you can't use it with multiple accounts
            username = keyring.get_password("kit-ilias-fuse", "username")
            password = keyring.get_password("kit-ilias-fuse", "password")
        except (ImportError, RuntimeError):
            logging.info("could not access keyring")

    if ask:
        username = username or input("Username: ")
        password = password or getpass.getpass()

    if None in (username, password):
        raise InvalidCredentialsError("no credentials available")

    return username, password


def save_user_pass(username, password):
    """attempt to save the username and password to the keyring"""
    with suppress(ImportError, RuntimeError):
        import keyring

        keyring.set_password("kit-ilias-fuse", "username", username)
        keyring.set_password("kit-ilias-fuse", "password", password)
        logging.info("saved login to keyring")


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


class IliasFSError(Exception):
    def __init__(self, message=None):
        self.message = message


class IliasFSNetworkError(IliasFSError): pass


class IliasFSParseError(IliasFSError):
    pass


def raise_ilias_error_to_fuse(e):
    logging.error(e)
    if type(e) == IliasSession.LoginError:
        raise FuseOSError(EACCES)
    elif type(e) == IliasFSParseError:
        raise FuseOSError(EREMOTEIO)
    raise FuseOSError(EIO)


class IliasSession(requests.Session):
    """
    Derivative of requests.Session for sessions at the ILIAS of Karlsruhe Institute of Technology.
    Attempts to log in to ILIAS via the Shibboleth Identity provider.

    Usage:
    >>> session = IliasSession(username="your_username", password=getpass()) #next you will be prompted for your password
    >>> session.get(some_course_url) #use it just like any requests.Session instance
    """

    class LoginError(IliasFSError):
        message = 'Error logging in to ilias.'

        def __init__(self):
            pass

    def __init__(self, cache_timeout, username, password, login_callback=None):
        super().__init__()
        self.username = username
        self.password = password
        self.login_callback = login_callback
        self.logger = logging.getLogger(type(self).__name__)
        self.cache_timeout = cache_timeout
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
        otp_inp = login_soup.find("input", attrs={"name": "j_tokenNumber"})
        if otp_inp:
            print("OTP Detected.")
            otp = input("OTP token: ")
            otp_url = otp_inp.parent.parent.parent['action']
            otp_response = self.post('https://idp.scc.kit.edu'+otp_url, data={'j_tokenNumber':otp, "_eventId_proceed": ""})
            login_soup = BeautifulSoup(otp_response.text, 'lxml')
        saml_response = None
        relay_state = None
        try:
            saml_response = login_soup.find("input", attrs={"name": "SAMLResponse"}).get("value")
            relay_state = login_soup.find("input", attrs={"name": "RelayState"}).get("value")
        except AttributeError as e:
            raise InvalidCredentialsError(
                "Username and/or password most likely invalid. (SAML response could not be found.)") from e

        if self.login_callback:
            self.login_callback(self.username, self.password)

        self.post("https://ilias.studium.kit.edu/Shibboleth.sso/SAML2/POST", data={
            "SAMLResponse": saml_response,
            "RelayState": relay_state
        })

    def get_ensure_login(self, url):
        kwargs = {
            "allow_redirects": False,
            "timeout": 10
        }
        try:
            resp = self.get(url, **kwargs)
            if resp.is_redirect:  # Try again
                self.login()
                resp = self.get(url, **kwargs)
                if resp.is_redirect:  # Didn't work...
                    raise IliasFSError()
            return resp
        except requests.RequestException as e:
            raise IliasFSNetworkError(e)


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
        self.__children = None
        self.__last_children_update = None

    def get_children(self):
        if self.__children is None or self.__last_children_update < time.time() - self.session.cache_timeout:
            self.__last_children_update = time.time()
            self.__children = {}
            node_page_req = self.session.get_ensure_login(self.url)
            try:
                node_page = node_page_req.text
                soup = BeautifulSoup(node_page, 'lxml')
                child_list_items = soup.select("div.il_ContainerListItem")
                for list_item in child_list_items:
                    a = list_item.select("a.il_ContainerItemTitle")
                    if len(a) == 0:
                        continue
                    a = a[0]
                    child_node = IliasNode.create_instance(a.text, a.get("href"), self.session, list_item)
                    logging.debug(child_node)
                    self.__children[child_node.name] = child_node
            except Exception as e:
                self.__children = None
                raise IliasFSParseError(e)
        return self.__children.values()

    def get_child_by_name(self, name):
        self.get_children()
        return self.__children.get(name) if self.__children is not None else None


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
            try:
                self.time = self.parse_date(properties[2].text.strip().lower())
            except ValueError:
                self.time = self.parse_date(properties[3].text.strip().lower())

    @staticmethod
    def parse_date(date_string):
        date_string = date_string.replace('heute', (datetime.datetime.now()).strftime('%d. %b %Y')) if 'heute' in date_string else date_string
        date_string = date_string.replace('gestern', (datetime.datetime.now() - datetime.timedelta(
                days=1)).strftime('%d. %b %Y')) if 'gestern' in date_string else date_string
        dtime = datetime.datetime.strptime(date_string, "%d. %b %Y, %H:%M")  # TODO Timezone?
        return time.mktime(dtime.timetuple())


    @staticmethod
    def human2bytes(s):
        symbols = ('Bytes', 'KB', 'MB', 'GB', 'TB')
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
        content = self.session.get_ensure_login(self.url).content
        cache.put(self, content)
        # Update size because size from overview is only approximate
        # Apparently this works
        self.size = len(content)
        return content[offset:offset + size]


class IliasDashboard(IliasNode):
    def __init__(self, session):
        super().__init__("Dashboard",
                         "https://ilias.studium.kit.edu/ilias.php?baseClass=ilPersonalDesktopGUI&cmd=jumpToSelectedItems",
                         session, None)


class IliasFS(LoggingMixIn, Operations):
    def __init__(self, root, dashboard):
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.dashboard = dashboard

    def __call__(self, op, path, *args):
        return super(IliasFS, self).__call__(op, path, *args)

    def access(self, path, mode):
        try:
            if self.__path_to_object(path):
                return 0
            else:
                raise FuseOSError(ENOENT)
        except IliasFSError as e:
            raise_ilias_error_to_fuse(e)

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
        try:
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
        except IliasFSError as e:
            raise_ilias_error_to_fuse(e)

    getxattr = None

    def readdir(self, path, fh):
        try:
            node = self.__path_to_object(path)
            return ['.', '..'] + [child.name for child in filter(lambda c: type(c) != IliasNode, node.get_children())]
        except IliasFSError as e:
            raise_ilias_error_to_fuse(e)

    def read(self, path, size, offset, fh):
        try:
            node = self.__path_to_object(path)
            if node:
                if type(node) == File:
                    val = node.download(size, offset)
                    return val
                else:
                    raise FuseOSError(EISDIR)
            else:
                raise FuseOSError(ENOENT)
        except IliasFSError as e:
            raise_ilias_error_to_fuse(e)


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

    cache_timeout_secs = args.cache_timeout * 60
    session = IliasSession(cache_timeout_secs, *get_user_pass(), login_callback=save_user_pass)

    dashboard = IliasDashboard(session)
    cache = Cache(capacity=args.cache, timeout=cache_timeout_secs)
    fuse = FUSE(IliasFS(args.mountpoint, dashboard), args.mountpoint, foreground=args.foreground)
