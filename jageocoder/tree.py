from collections import OrderedDict
import csv
from logging import getLogger
import os
import re
import site
import sys
from typing import Union, NoReturn, List, Optional, TextIO

from deprecated import deprecated
from sqlalchemy import Index
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

import jageocoder
from jageocoder.address import AddressLevel
from jageocoder.base import Base
from jageocoder.exceptions import AddressTreeException
from jageocoder.itaiji import converter as itaiji_converter
from jageocoder.node import AddressNode
from jageocoder.result import Result
from jageocoder.trie import AddressTrie, TrieNode

logger = getLogger(__name__)


def get_db_dir(mode: str = 'r') -> os.PathLike:
    """
    Get the database directory.

    Parameters
    ----------
    mode: str, optional(default='r')
        Specifies the mode for searching the database directory.
        If 'a' or 'w' is set, search a writable directory.
        If 'r' is set, search a database file that already exists.

    Return
    ------
    The path to the database directory.
    If no suitable directory is found, raise an AddressTreeException.

    Notes
    -----
    This method searches a directory in the following order of priority.
    - 'JAGEOCODER_DB_DIR' environment variable
    - '(sys.prefix)/jageocoder/db/'
    - '(site.USER_BASE)/jageocoder/db/'
    """
    if mode not in ('a', 'w', 'r'):
        raise AddressTreeException(
            'Invalid mode value. Specify one of "a", "w", or "r".')

    db_dirs = []
    if 'JAGEOCODER_DB_DIR' in os.environ:
        db_dirs.append(os.environ['JAGEOCODER_DB_DIR'])

    db_dirs += [
        os.path.join(sys.prefix, 'jageocoder/db/'),
        os.path.join(site.USER_BASE, 'jageocoder/db/'),
    ]

    for db_dir in db_dirs:
        path = os.path.join(db_dir, 'address.db')
        if os.path.exists(path):
            return db_dir

        if mode == 'r':
            continue

        try:
            os.makedirs(db_dir, mode=0o777, exist_ok=True)
            fp = open(path, 'a')
            fp.close()
            return db_dir
        except (FileNotFoundError, PermissionError):
            continue

    return None


class LRU(OrderedDict):
    'Limit size, evicting the least recently looked-up key when full'

    def __init__(self, maxsize=512, *args, **kwds):
        self.maxsize = maxsize
        super().__init__(*args, **kwds)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)

        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            oldest = next(iter(self))
            logger.debug("Delete '{}'".format(oldest))
            del self[oldest]


class AddressTree(object):
    """
    The address-tree structure.

    Attributes
    ----------
    db_path: str
        Path to the sqlite3 database file.
    dsn: str
        RFC-1738 based database-url, so called "data source name".
    trie_path: str
        Path to the TRIE index file.
    engine : sqlalchemy.engine.Engine
        The database engine which is used to connect to the database.
    conn : sqlalchemy.engine.Connection
        The connection object which is used to communicate witht the database.
    session : sqlalchemy.orm.Session
        The session object used for a series of database operations.
    root : AddressNode
        The root node of the tree.
    trie : AddressTrie
        The TRIE index of the tree.
    mode: str
        The mode in which this tree was opened.
    """

    def __init__(self, dsn: Optional[str] = None,
                 trie_path: Optional[os.PathLike] = None,
                 db_dir: Optional[os.PathLike] = None,
                 mode: str = 'a',
                 debug: bool = False):
        """
        The initializer

        Parameters
        ----------
        dsn: str, optional
            Data Source Name of the database.
        trie_path: os.PathLike, optional
            File path to save the TRIE index.
        db_dir: os.PathLike, optional
            The database directory.
            If dsn and trie_path are omitted and db_dir is set,
            'address.db' and 'address.trie' under this directory will be used.
        mode: str (default='a')
            Specifies the mode for opening the database.
            - In the case of 'a', if the database already exists,
              use it. Otherwize create a new one.
            - In the case of 'w', if the database already exists,
              delete it first. Then create a new one.
            - In the case of 'r', if the database already exists,
              use it. Otherwise raise a JageocoderError exception.
        debug: bool default=False)
            Debugging flag.
        """
        # Set default values
        self.mode = mode
        if db_dir is None:
            db_dir = get_db_dir(mode)
        else:
            db_dir = os.path.abspath(db_dir)

        default_dsn = 'sqlite:///' + os.path.join(db_dir, 'address.db')
        default_trie_path = os.path.join(db_dir, 'address.trie')

        self.dsn = dsn or default_dsn
        self.db_path = self.dsn[len('sqlite:///'):]
        self.trie_path = trie_path or default_trie_path

        # Options
        self.debug = debug

        # Clear database?
        if self.mode == 'w':
            if os.path.exists(self.db_path):
                os.remove(self.db_path)

            if os.path.exists(self.trie_path):
                os.remove(self.trie_path)

        # Database connection
        try:
            self.engine = create_engine(self.dsn, echo=self.debug)
            self.conn = self.engine.connect()
            _session = sessionmaker()
            _session.configure(bind=self.engine)
            self.session = _session()
        except Exception as e:
            logger.error(e)
            exit(1)

        self.root = None
        self.trie = AddressTrie(self.trie_path)

        # Regular expression
        self.re_float = re.compile(r'^\-?\d+\.?\d*$')
        self.re_int = re.compile(r'^\-?\d+$')
        self.re_address = re.compile(r'^(\d+);(.*)$')

        # Check database version
        if self.mode == 'w':
            self.__create_db()

        db_version = self.get_version()
        if not self.is_version_compatible():
            logger.warning((
                "The database ({}) is not compatible with the module ({})."
            ).format(db_version, jageocoder.dictionary_version()))

    def close(self) -> NoReturn:
        if self.session:
            self.session.close()

        if self.engine:
            self.engine.dispose()
            del self.engine
            self.engine = None

    def is_version_compatible(self) -> bool:
        """
        Check if the dictionary version is compatible with the package.

        Returns
        -------
        bool
            True if compatible, otherwize False.
        """
        current_dict_ver = self.get_version()
        required_dict_ver = jageocoder.dictionary_version()
        if current_dict_ver != required_dict_ver:
            return False

        return True

    def __not_in_readonly_mode(self) -> NoReturn:
        """
        Check if the dictionary is not opened in the read-only mode.

        If the mode is read-only, AddressTreeException will be raised.
        """
        if self.mode == 'r':
            raise AddressTreeException(
                'This method is not available in read-only mode.')

    def __create_db(self) -> NoReturn:
        """
        Create database and tables.
        """
        self.__not_in_readonly_mode()
        Base.metadata.create_all(self.engine)
        root = self.get_root()
        root.save_recursive(self.session)
        self.session.commit()

    def get_session(self) -> Session:
        """
        Get the database session.

        Returns
        -------
        sqlalchemy.orm.Session:
            The current session object.
        """
        return self.session

    def get_root(self) -> AddressNode:
        """
        Get the root-node of the tree.
        If not set yet, create and get the node from the database.

        Returns
        -------
        AddressNode:
            The root node object.
        """
        if self.root:
            return self.root

        # Try to get root from the database
        try:
            self.root = self.session.query(
                AddressNode).filter_by(id=-1).one()
        except NoResultFound:
            # Create a new root
            self.root = AddressNode(
                id=-1, name="_root_", parent_id=None,
                note=jageocoder.dictionary_version())

        return self.root

    def get_version(self) -> str:
        """
        Get the version of the tree file.

        Return
        ------
        str:
            The version string.
        """
        root_node = self.get_root()
        if root_node.note is None:
            return "(no version)"

        return root_node.note

    def get_node_by_id(self, node_id: int) -> AddressNode:
        """
        Get the full node information by its id.

        Parameters
        ----------
        node_id: int
            The target node id.

        Return
        ------
        AddressNode
        """
        return self.session.query(AddressNode).get(node_id)

    def get_node_fullname(self, node: Union[AddressNode, int]) -> List[str]:
        if isinstance(node, AddressNode):
            node_id = node.id
        else:
            node_id = node

        names = []
        while node_id >= 0:
            node = self.session.execute(
                'SELECT parent_id, name FROM node WHERE id={}'.format(
                    node_id)).one()
            names.insert(0, node.name)
            node_id = node.parent_id

        return names

    def check_line_format(self, args: List[str]) -> int:
        """
        Receives split args from a line of comma-separated text
        representing a single address element, and returns
        the format ID.

        Parameters
        ----------
        args: list[str]

        Return
        ------
        int
            The id of the identified format.
            1. Address names without level, lon, lat
            2. Address names without level, lon, lat, note
            3. Address names without level, lon, lat, level without note
            4. Address names without level, lon, lat, level, note

        Examples
        --------
        >>> from jageocoder_converter import BaseConverter
        >>> base = BaseConverter()
        >>> base.check_line_format(['1;北海道','3;札幌市','4;中央区','141.34103','43.05513'])
        1
        >>> base.check_line_format(['1;北海道','3;札幌市','4;中央区','5;大通','6;西二十丁目','141.326249','43.057218','01101/ODN-20/'])
        2
        >>> base.check_line_format(['北海道','札幌市','中央区','大通','西二十丁目','141.326249','43.057218',6])
        3
        >>> base.check_line_format(['北海道','札幌市','中央区','大通','西二十丁目','141.326249','43.057218',6,'01101/ODN-20/'])
        4
        """

        # Find the first consecutive position of a real number or None.
        pos0 = None
        pos1 = None
        for pos, arg in enumerate(args):
            if arg == '' or self.re_float.match(arg):
                if pos0:
                    pos1 = pos
                    break
                else:
                    pos0 = pos
            else:
                pos0 = None

        if pos1 is None:
            raise AddressTreeException(
                'Unexpected line format.\n{}'.format(','.join(args)))

        names = args[0:pos0]

        if self.re_address.match(names[0]):
            if len(args) == pos1 + 1:
                logger.debug("line format id: 1")
                return 1

            if len(args) == pos1 + 2:
                logger.debug("line format id: 2")
                return 2

        else:
            if len(args) == pos1 + 2 and self.re_int.match(args[pos1 + 1]):
                logger.debug("line format id: 3")
                return 3

            if len(args) == pos1 + 3 and self.re_int.match(args[pos1 + 1]):
                logger.debug("line format id: 4")
                return 4

        raise AddressTreeException(
            'Unexpected line format.\n{}'.format(','.join(args)))

    def parse_line_args(self, args: List[str], format_id: int) -> list:
        """
        Receives split args from a line of comma-separated text
        representing a single address element, and returns
        a list of parsed attributes.

        Parameters
        ----------
        args: list[str]
            List of split args in a line
        format_id: int
            The id of the line format identfied by `check_line_format`

        Return
        ------
        list
            A list containing the following attributes.
            - Address names: list[str]
            - Longitude: float
            - Latitude: float
            - Level: int or None
            - note: str or None

        Examples
        --------
        >>> from jageocoder_converter import BaseConverter
        >>> base = BaseConverter()
        >>> base.parse_line_args(['1;北海道','3;札幌市','4;中央区','141.34103','43.05513'], 1)
        [['1;北海道','3;札幌市','4;中央区'], 141.34103, 43.05513, None, None]
        >>> base.parse_line_args(['1;北海道','3;札幌市','4;中央区','5;大通','6;西二十丁目','141.326249','43.057218','01101/ODN-20/'], 2)
        [['1;北海道','3;札幌市','4;中央区','5;大通','6;西二十丁目'],141.326249,43.057218,None,'01101/ODN-20/']
        >>> base.parse_line_args(['北海道','札幌市','中央区','大通','西二十丁目','141.326249','43.057218',6,'01101/ODN-20/'], 4)
        [['北海道','札幌市','中央区','大通','西二十丁目'],141.326249,43.057218,6,'01101/ODN-20/']
        """

        def fv(val: str) -> Union[float, None]:
            """
            Convert str to float value.
            If the str is empty, return None.
            """
            if val == '':
                return None
            return float(val)

        nargs = len(args)
        if format_id == 1:
            return [
                args[0:nargs-2], fv(args[nargs-2]), fv(args[nargs-1]),
                None, None]

        if format_id == 2:
            return [
                args[0:nargs-3], fv(args[nargs-3]), fv(args[nargs-2]),
                None, args[nargs-1]]

        if format_id == 3:
            return [
                args[0:nargs-3], fv(args[nargs-3]), fv(args[nargs-2]),
                int(args[nargs-1]), None]

        if format_id == 4:
            return [
                args[0:nargs-4], fv(args[nargs-4]), fv(args[nargs-3]),
                int(args[nargs-2]), args[nargs-1]]

        raise AddressTreeException(
            'Unexpected line format id: {}'.format(format_id))

    def add_address(self,
                    address_names: List[str],
                    do_update: bool = False,
                    cache: Optional[LRU] = None,
                    **kwargs) -> AddressNode:
        """
        Create a new AddressNode and add to the tree.

        Parameters
        ----------
        address_names : list of str
            A list of the address element names.
            For example, ["東京都","新宿区","西新宿", "２丁目"]
        do_update : bool
            When an address with the same name already exists,
            update it with the value of kwargs if 'do_update' is true,
            otherwise do nothing.
        cache : LRU, optional
            A dict object to use as a cache for improving performance,
            whose keys are the address notation from the prefecture level
            and whose values are the corresponding nodes.
            If not specified or None is given, do not use the cache.
        **kwargs : properties of the new address node.
            x : float. X coordinate or longitude in decimal degree
            y : float. Y coordinate or latitude in decimal degree
            level: int. Level of the node
            note : str. Note

        Return
        ------
        AddressNode:
            The added node.
        """
        self.__not_in_readonly_mode()
        cur_node = self.get_root()
        for i, elem in enumerate(address_names):
            path = ''.join(address_names[0:i + 1])
            is_leaf = (i == len(address_names) - 1)

            if cache is not None:
                if path in cache:
                    cur_node = cache[path]
                    continue
                elif i < len(address_names):
                    logger.debug("Cache miss: '{}'".format(path))

            m = self.re_address.match(elem)
            if m:
                level = m.group(1)
                name = m.group(2)
            else:
                level = AddressLevel.UNDEFINED
                name = elem
                if is_leaf:
                    level = kwargs.get('level', AddressLevel.UNDEFINED)

            name_index = itaiji_converter.standardize(name)
            node = cur_node.get_child(name_index)
            if not node:
                kwargs.update({
                    'name': name,
                    'parent': cur_node,
                    'level': level})
                new_node = AddressNode(**kwargs)
                cur_node.add_child(new_node)
                cur_node = new_node
            else:
                cur_node = node
                if is_leaf:
                    if do_update:
                        cur_node.set_attributes(**kwargs)
                    else:
                        cur_node = None

            if cache is not None and \
                    cur_node is not None and \
                    not is_leaf:
                cache[path] = cur_node

        return cur_node

    def update_name_index(self) -> int:
        """
        Update `name_index` field using the standardizing logic
        of the current version.

        Note
        ----
        This method also updates the version information of
        the dictionary.

        Return
        ------
        int:
            Number of records updated.
        """
        self.__not_in_readonly_mode()
        counts = self.session.query(AddressNode).count()
        pct = 0
        pagesize = int(counts / 100) + 1
        diffs = 0
        for offset in range(0, counts, pagesize):
            nodes = self.session.query(
                AddressNode).offset(offset).limit(pagesize)
            for node in nodes:
                new_name_index = itaiji_converter.standardize(node.name)
                if node.name_index != new_name_index:
                    logger.info((
                        'The index of "{}" was updated from "{}" to "{}"'
                    ).format(node.name, node.name_index, new_name_index))
                    node.name_index = new_name_index
                    self.session.add(node)
                    diffs += 1

            self.session.commit()
            pct += 1
            logger.info("Updated {pct}% ({offset}/{total})".format(
                pct=pct, offset=offset + pagesize, total=counts))

        logger.info("Update completed.")
        # Update version
        root_node = self.get_root()
        root_node.note = jageocoder.dictionary_version()
        self.session.add(root_node)
        self.session.commit()

        return diffs

    def create_trie_index(self) -> NoReturn:
        """
        Create the TRIE index from the tree.
        """
        self.__not_in_readonly_mode()
        self.index_table = {}
        logger.debug("Collecting labels for the trie index...")
        self._get_index_table()

        logger.debug("Building Trie...")
        self.trie = AddressTrie(self.trie_path, self.index_table)
        self.trie.save()

        self._set_index_table()

    def _get_index_table(self) -> NoReturn:
        """
        Collect the names of all address elements
        to be registered in the TRIE index.
        The collected notations will be stored in `tree.index_table`.

        Generates notations that describe everything from the name of
        the prefecture to the name of the oaza without abbreviation,
        notations that omit the name of the prefecture, or notations
        that omit the name of the prefecture and the city.
        """
        # Build temporary lookup table
        logger.debug("Building temporary lookup table..")
        tmp_id_name_table = {}
        for node in self.session.query(
            AddressNode.id, AddressNode.name, AddressNode.name_index,
            AddressNode.parent_id).filter(
                AddressNode.level <= AddressLevel.OAZA):
            tmp_id_name_table[node.id] = node

        logger.debug("  {} records found.".format(
            len(tmp_id_name_table)))

        # Create index_table
        self.index_table = {}
        for k, v in tmp_id_name_table.items():
            node_prefixes = []
            cur_node = v
            while True:
                node_prefixes.insert(0, cur_node.name)
                if cur_node.parent_id < 0:
                    break

                if cur_node.parent_id not in tmp_id_name_table:
                    raise RuntimeError(
                        ('The parent_id:{} of node:{} is not'.format(
                            cur_node.parent_id, cur_node),
                         ' in the tmp_id_table'))

                cur_node = tmp_id_name_table[cur_node.parent_id]

            for i in range(len(node_prefixes)):
                label = ''.join(node_prefixes[i:])
                label_standardized = itaiji_converter.standardize(
                    label)
                if label_standardized in self.index_table:
                    self.index_table[label_standardized].append(v.id)
                else:
                    self.index_table[label_standardized] = [v.id]

            # Also register variant notations for node labels
            for candidate in itaiji_converter.standardized_candidates(
                    v.name_index):
                if candidate == v.name_index:
                    # The original notation has been already registered
                    continue

                if candidate in self.index_table:
                    self.index_table[candidate].append(v.id)
                else:
                    self.index_table[candidate] = [v.id]

        self.session.commit()

    def _set_index_table(self) -> NoReturn:
        """
        Map all the id of the TRIE index (TRIE id) to the node id.

        Collect notations recursively the names of all address elements
        which was registered in the TRIE index, retrieve
        the id of each notations in the TRIE index,
        then add the TrieNode to the database that maps
        the TRIE id to the node id.
        """
        logger.debug("Creating mapping table from trie_id:node_id")
        logger.debug("  Deleting old TrieNode table...")
        self.session.query(TrieNode).delete()
        logger.debug("  Dropping index...")
        try:
            self.session.execute("DROP INDEX ix_trienode_trie_id")
        except OperationalError:
            logger.debug("    the index does not exist. (ignored)")

        logger.debug("  Adding mapping records...")
        for k, node_id_list in self.index_table.items():
            trie_id = self.trie.get_id(k)
            for node_id in node_id_list:
                tn = TrieNode(trie_id=trie_id, node_id=node_id)
                self.session.add(tn)

        logger.debug("  Creating index on trienode.trie_id ...")
        trienode_trie_id_index = Index(
            'ix_trienode_trie_id', TrieNode.trie_id)
        try:
            trienode_trie_id_index.create(self.engine)
        except OperationalError:
            logger.debug("  the index already exists. (ignored)")

        self.session.commit()
        logger.debug("  done.")

    def save_all(self) -> NoReturn:
        """
        Save all AddressNode in the tree to the database.
        """
        self.__not_in_readonly_mode()
        logger.debug("Starting save full tree (recursive)...")
        self.get_root().save_recursive(self.session)
        self.session.commit()
        logger.debug("Finished save tree.")

    def read_file(self, path: os.PathLike,
                  do_update: bool = False) -> NoReturn:
        """
        Add AddressNodes from a text file.
        See 'data/test.txt' for the format of the text file.

        Parameters
        ----------
        path : os.PathLike
            Text file path.
        do_update : bool (default=False)
            When an address with the same name already exists,
            update it with the value of the new data
            if 'do_update' is true, otherwise do nothing.
        """
        raise AddressTreeException(
            'This method is not available in read-only mode.')
        logger.debug("Starting read_file...")
        with open(path, 'r', encoding='utf-8',
                  errors='backslashreplace') as f:
            self.read_stream(f, do_update=do_update)

    def read_stream(self, fp: TextIO,
                    do_update: bool = False) -> NoReturn:
        """
        Add AddressNodes to the tree from a stream.

        Parameters
        ----------
        fp : io.TextIO
            Input text stream.
        do_update : bool (default=False)
            When an address with the same name already exists,
            update it with the value of the new data
            if 'do_update' is true, otherwise do nothing.
        """
        self.__not_in_readonly_mode()
        nread = 0
        stocked = []
        prev_names = None
        cache = LRU(maxsize=512)

        reader = csv.reader(fp)
        format_id = None

        while True:
            try:
                args = reader.__next__()
            except UnicodeDecodeError:
                logger.error("Decode error at the next line of {}".format(
                    prev_names))
                exit(1)
            except StopIteration:
                break

            if args is None:
                break

            if format_id is None:
                format_id = self.check_line_format(args)

            names, lon, lat, level, note = self.parse_line_args(
                args, format_id)

            if names[-1].startswith('!'):
                names = names[0:-1]

            if prev_names == names:
                logger.debug("Skipping '{}".format(prev_names))
                continue

            prev_names = names

            node = self.add_address(
                names, do_update, cache=cache,
                x=lon, y=lat, level=level, note=note)
            nread += 1
            if nread % 1000 == 0:
                logger.info("- read {} lines.".format(nread))

            if node is None:
                # The node exists and not updated.
                continue

            stocked.append(node)
            if len(stocked) > 10000:
                logger.debug("Inserting into the database... ({} - {})".format(
                    stocked[0].get_fullname(), stocked[-1].get_fullname()))
                for node in stocked:
                    self.session.add(node)

                self.session.commit()
                stocked.clear()

        logger.debug("Finished reading the stream.")
        if len(stocked) > 0:
            logger.debug("Inserting into the database... ({} - {})".format(
                stocked[0].get_fullname(), stocked[-1].get_fullname()))
            for node in stocked:
                self.session.add(node)

            self.session.commit()

        logger.debug("Done.")

    def drop_indexes(self) -> NoReturn:
        """
        Drop indexes to improve the speed of bulk insertion.
        - ix_node_parent_id ON node (parent_id)
        - ix_trienode_trie_id ON trienode (trie_id)
        """
        self.__not_in_readonly_mode()
        logger.debug("Dropping indexes...")
        self.session.execute("DROP INDEX ix_node_parent_id")
        logger.debug("  done.")

    def create_tree_index(self) -> NoReturn:
        """
        Add index later that were not initially defined.
        - ix_node_parent_id ON node (parent_id)
        """
        self.__not_in_readonly_mode()
        logger.debug("Creating index on node.parent_id ...")
        node_parent_id_index = Index(
            'ix_node_parent_id', AddressNode.parent_id)
        try:
            node_parent_id_index.create(self.engine)
        except OperationalError:
            logger.warning("  the index already exists. (ignored)")

        logger.debug("  done.")

    def search_by_tree(self, address_names: List[str]) -> AddressNode:
        """
        Get the corresponding node id from the list of address element names,
        recursively search for child nodes using the tree.

        For example, ['東京都','新宿区','西新宿','二丁目'] will search
        the '東京都' node under the root node, search the '新宿区' node
        from the children of the '東京都' node. Repeat this process and
        return the '二丁目' node which is a child of '西新宿' node.

        Parameters
        ----------
        address_names : list of str
            A list of address element names to be searched.

        Return
        ------
        AddressNode:
            The node matched last.
        """
        cur_node = self.get_root()
        for name in address_names:
            name_index = itaiji_converter.standardize(name)
            node = cur_node.get_child(name_index)
            if not node:
                break
            else:
                cur_node = node

        return cur_node

    def search_by_trie(self, query: str,
                       best_only: bool = True) -> dict:
        """
        Get the list of corresponding nodes using the TRIE index.
        Returns a list of address element nodes that match
        the query string in the longest part from the beginning.

        For example, '中央区中央1丁目' will return the nodes
        corresponding to '千葉県千葉市中央区中央一丁目' and
        '神奈川県相模原市中央区中央一丁目'.

        Parameters
        ----------
        query : str
            An address notation to be searched.
        best_only : bool (option, default=True)
            If true, get the best candidates will be returned.

        Return
        ------
        A dict object whose key is a node id
        and whose value is a list of node and substrings
        that match the query.
        """
        index = itaiji_converter.standardize(
            query, keep_numbers=True)
        index_for_trie = itaiji_converter.standardize(query)
        candidates = self.trie.common_prefixes(index_for_trie)
        results = {}
        max_len = 0
        min_part = None

        keys = sorted(candidates.keys(),
                      key=len, reverse=True)

        logger.debug("Trie: {}".format(','.join(keys)))

        min_key = ''
        processed_nodes = []

        for k in keys:
            if len(k) < len(min_key):
                logger.debug("Key '{}' is shorter than '{}'".format(
                    k, min_key))
                continue

            trie_id = candidates[k]
            logger.debug("Trie_id of key '{}' = {}".format(
                k, trie_id))
            trienodes = self.session.query(
                TrieNode).filter_by(trie_id=trie_id).all()
            offset = itaiji_converter.match_len(index, k)
            key = index[0:offset]
            rest_index = index[offset:]
            for trienode in trienodes:
                node = trienode.node

                if min_key == '' and node.level < AddressLevel.WARD:
                    # To make the process quicker, once a node higher
                    # than the city level is found, addresses shorter
                    # than the node are not searched after this.
                    logger.debug((
                        "A node with city or higher levels found. "
                        "Set min_key to '{}'").format(k))
                    min_key = k

                if node.id in processed_nodes:
                    logger.debug("Node {}({}) already processed.".format(
                        node.name, node.id))
                    continue

                logger.debug("Search '{}' under {}({})".format(
                    rest_index, node.name, node.id))
                results_by_node = node.search_recursive(
                    rest_index, self.session, processed_nodes)
                processed_nodes.append(node.id)
                logger.debug('{}({}) marked as processed'.format(
                    node.name, node.id))

                for cand in results_by_node:
                    """
                    if cand[1] != '':
                        cur_node = cand[0]
                        while cur_node.parent:
                            logger.debug('{}({}) marked as processed'.format(
                                cur_node.name, cur_node.id))
                            processed_nodes.append(cur_node.id)
                            cur_node = cur_node.parent
                    """

                    _len = offset + cand.nchars
                    _part = offset + len(cand.matched)
                    msg = "candidate: {} ({})"
                    logger.debug(msg.format(key + cand.matched, _len))
                    if best_only:
                        if _len > max_len:
                            results = {
                                "cand.node.id": [cand.node, key + cand.matched]
                            }
                            max_len = _len
                            min_part = _part

                        elif _len == max_len and cand.node.id not in results \
                                and (min_part is None or _part <= min_part):
                            results[cand.node.id] = [
                                cand.node, key + cand.matched]
                            min_part = _part

                    else:
                        results[cand.node.id] = [cand.node, key + cand[1]]
                        max_len = max(_len, max_len)
                        if min_part is None:
                            min_part = _part
                        else:
                            min_part = min(min_part, _part)

        logger.debug(AddressNode.search_child_with_criteria.cache_info())
        return results

    @deprecated(('Renamed to `searchNode()` because it was confusing'
                 ' with jageocoder.search()'))
    def search(self, query: str, **kwargs) -> list:
        return self.searchNode(query, **kwargs)

    def searchNode(self, query: str,
                   best_only: bool = True) -> List[Result]:
        """
        Searches for address nodes corresponding to an address notation
        and returns the matching substring and a list of nodes.

        Parameters
        ----------
        query : str
            An address notation to be searched.
        best_only: bool (default = True)
            If set to False, Returns all candidates whose prefix matches.

        Return
        ------
        list:
            A list of AddressNode and matched substring pairs.

        Note
        ----
        The `search_by_trie` function returns the standardized string
        as the match string. In contrast, the `searchNode` function
        returns the de-starndized string.

        Example
        -------
        >>> import jageocoder
        >>> jageocoder.init()
        >>> tree = jageocoder.get_module_tree()
        >>> tree.searchNode('多摩市落合1-15-2')
        [[[11460207:東京都(139.69178,35.68963)1(lasdec:130001/jisx0401:13)]>[12063502:多摩市(139.446366,35.636959)3(jisx0402:13224)]>[12065383:落合(139.427097,35.624877)5(None)]>[12065384:一丁目(139.427097,35.624877)6(None)]>[12065390:15番地(139.428969,35.625779)7(None)], '多摩市落合1-15-']]
        """
        results = self.search_by_trie(query, best_only)
        values = sorted(results.values(), reverse=True,
                        key=lambda v: len(v[1]))

        matched_substring = {}
        results = []
        for v in values:
            if v[1] in matched_substring:
                matched = matched_substring[v[1]]
            else:
                matched = self._get_matched_substring(query, v)
                matched_substring[v[1]] = matched

            results.append(Result(v[0], matched))

        # Sort the results in descending order by node.level.
        results.sort(key=lambda r: r.node.level, reverse=True)

        return results

    def _get_matched_substring(
            self, query: str,
            retrieved: list) -> str:
        """
        From the matched standardized substring,
        recover the corresponding substring of
        the original search string.

        Parameters
        ----------
        query : str
            The original search string.
        matchd : str
            The matched standardized substring.

        Return
        ------
        str:
            The recovered substring.
        """
        recovered = None
        node, matched = retrieved
        l_result = len(matched)
        pos = l_result if l_result <= len(query) else len(query)
        pos_history = [pos]

        while True:
            substr = query[0:pos]
            standardized = itaiji_converter.standardize(
                substr, keep_numbers=True)
            l_standardized = len(standardized)

            if l_standardized == l_result:
                recovered = substr
                break

            if l_standardized <= l_result:
                pos += 1
            else:
                pos -= 1

            if pos < 0 or pos > len(query):
                break

            if pos in pos_history:
                message = "Can't de-standardize matched {} in {}".format(
                    matched, query)
                raise AddressTreeException(message)

        if pos < len(query) and node.name != '':
            if query[pos] == node.name[-1] and \
                    len(itaiji_converter.standardize(
                        query[0:pos+1])) == l_result:
                # When the last letter of a node name is omitted
                # by normalization, and if the query string contains
                # that letter, it is determined to have matched
                # up to that letter.
                # Ex. "兵庫県宍粟市山崎町上ノ１５０２" will match "上ノ".
                recovered = query[0:pos+1]
            elif query[-2:] in ('通り', '通リ'):
                # '通' can be expressed as '通り'
                recovered = query[0:pos+1]

        return recovered
