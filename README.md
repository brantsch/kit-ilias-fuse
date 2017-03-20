# ilias-fuse
*Finally, a FUSE filesystem for the ILIAS installation at the KIT!*
It enables you to mount your view of ILIAS on some directory so that you
can browse the files and folders of your ILIAS courses with your shell or file
manager and access them (almost) like local files.

# Cloning
Please use `git clone`.

# Dependencies
- requests
- BeautifulSoup
- pyfuse
- and, of course, Python 3

# Usage
```
% ilias-fuse.py /path/to/your/mountpoint
```

For advanced options, see the output of
```
% ilias-fuse.py --help
```

# Caveats
1. **This is an immature and crude hack.** (But by using it you might uncover problems I haven't found yet.)
2. Only files and directories are supported, as I did not find a proper FS abstraction of ILIAS's excercises.
3. File sizes of text files may be too small (ILIAS bug?).

# Contribute
- Please send me your pull requests!
- In case you know someone responsible for ILIAS at the KIT, please kindly ask
  them to enable the WebDAV support of ILIAS so that this crude hack can be
  deprecated in favour of the correct solution.
