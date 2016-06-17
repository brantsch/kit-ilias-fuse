#ilias-fuse
*Finally, a FUSE filesystem for the ILIAS installation at the KIT!*
It enables you to mount your view of ILIAS on some directory so that you
can browse the files and folders of your ILIAS courses with your shell or file
manager and access them (almost) like local files.

#Usage
```
% ilias-fuse.py /path/to/your/mountpoint
```

###### Caveats
Only files and directories are supported, as I did not find a proper FS abstraction of ILIAS's excercises.
File sizes of text files may be too small (ILIAS bug?).
Acces, Modification and Change times of files will all be set to 1970-01-01 00:00.

# Contribute
- Please send me your pull requests!
- In case you know someone responsible for ILIAS at the KIT, please kindly ask
  them to enable the WebDAV support of ILIAS so that this crude hack can be
  deprecated in favour of the correct solution.
