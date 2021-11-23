import json
from logging import getLogger
import os
from typing import Union

from jageocoder.address import AddressLevel
from jageocoder.strlib import strlib

logger = getLogger(__name__)


class Converter(object):

    optional_prefixes = ['字', '大字', '小字']
    # Optional postfixes for each address level
    optional_postfixes = {
        AddressLevel.CITY: ['市', '区', '町', '村'],
        AddressLevel.WARD: ['区'],
        AddressLevel.OAZA: ['町', '条', '線', '丁', '丁目', '番', '号'],
        AddressLevel.AZA: ['町', '条', '線', '丁', '丁目', '区', '番', '号'],
        AddressLevel.BLOCK: ['番', '番地'],
        AddressLevel.BLD: ['号', '番地'],
    }

    kana_letters = (strlib.HIRAGANA, strlib.KATAKANA)
    latin1_letters = (strlib.ASCII, strlib.NUMERIC, strlib.ALPHABET)

    def __init__(self):
        """
        Initialize the converter.

        Attributes
        ----------
        trans_itaiji: table
            The character mapping table from src to dst.
        """
        itaiji_dic_json = os.path.join(
            os.path.dirname(__file__), 'itaiji_dic.json')

        with open(itaiji_dic_json, 'r', encoding='utf-8') as f:
            itaiji_dic = json.load(f)

        src_str, dst_str = '', ''
        for src, dst in itaiji_dic.items():
            src_str += src
            dst_str += dst

        self.trans_itaiji = str.maketrans(src_str, dst_str)
        self.trans_h2z = str.maketrans(
            {chr(0x0021 + i): chr(0xFF01 + i) for i in range(94)})
        self.trans_z2h = str.maketrans(
            {chr(0xFF01 + i): chr(0x21 + i) for i in range(94)})

    def check_optional_prefixes(self, notation: str) -> int:
        """
        Check optional prefixes in the notation and
        return the length of the prefix string.

        Parameters
        ----------
        notation: str
            The address notation to be checked.

        Return
        ------
        int
            The length of optional prefixes string.

        Examples
        --------
        >>> from jageocoder.itaiji import converter
        >>> converter.check_optional_prefixes('大字道仏')
        2
        >>> converter.check_optional_prefixes('字貝取')
        1
        """
        for prefix in self.__class__.optional_prefixes:
            if notation.startswith(prefix):
                return len(prefix)

        return 0

    def check_optional_postfixes(self, notation: str, level: int) -> int:
        """
        Check optional postfixes in the notation and
        return the length of the postfix string.

        Parameters
        ----------
        notation : str
            The address notation to be checked.
        level: int
            Address level of the target element.

        Return
        ------
        int
            The length of optional postfixes string.

        Examples
        --------
        >>> from jageocoder.itaiji import converter
        >>> converter.check_optional_postfixes('1番地', 7)
        2
        >>> converter.check_optional_postfixes('15号', 8)
        1
        """
        if level not in self.__class__.optional_postfixes:
            return 0

        for postfix in self.__class__.optional_postfixes[level]:
            if notation.endswith(postfix):
                return len(postfix)

        return 0

    def standardize(self, notation: Union[str, None],
                    keep_numbers: bool = False) -> str:
        """
        Standardize an address notation.

        Parameters
        ----------
        notation : str
            The address notation to be standardized.
        keep_numbers: bool, optional
            If set to True, do not process numerical characters.

        Return
        ------
        str
            The standardized address notation string.

        Examples
        --------

        """
        if notation is None or len(notation) == 0:
            return notation

        l_optional_prefix = self.check_optional_prefixes(notation)
        notation = notation[l_optional_prefix:]

        notation = notation.translate(
            self.trans_itaiji).translate(self.trans_z2h).upper()

        notation = notation.replace('通り', '通')

        prectype, ctype, nctype = strlib.ASCII, strlib.ASCII, strlib.ASCII
        new_notation = ""
        i = 0

        while i < len(notation):
            c = notation[i]
            prectype = ctype
            if i == 0:
                ctype = strlib.get_ctype(c)
            else:
                ctype = nctype

            if i == len(notation) - 1:
                nctype = strlib.ASCII
            else:
                nctype = strlib.get_ctype(notation[i + 1])

            # logger.debug(
            #    "c:{c}, pret:{prectype}, ct:{ctype}, nt:{nctype}".format(
            #    c = c, prectype = prectype, ctype = ctype, nctype = nctype))

            """
            # 'ノ' and 'の' between numeric or ascii letters
            # are treated as hyphens.
            if c in 'ノの' and prectype in self.latin1_letters and \
                    nctype in self.latin1_letters:
                new_notation += '-'
                ctype = strlib.ASCII
                i += 1
                continue

            # Remove optional characters when placed between
            # characters except Kana.
            if c in 'ケヶガがツッつノの' and \
               prectype not in self.kana_letters and \
               nctype not in self.kana_letters:
                ctype = prectype
                i += 1
                continue
            """

            # Replace hyphen-like characters with '-'
            if strlib.is_hyphen(c):
                new_notation += '-'
                ctype = strlib.ASCII
                i += 1
                continue

            # Replace numbers including Chinese characters
            # with number + '.' in the notation.
            if not keep_numbers and strlib.get_numeric_char(c):
                ninfo = strlib.get_number(notation[i:])
                new_notation += str(ninfo['n']) + '.'
                i += ninfo['i']
                if i < len(notation) and notation[i] == '.':
                    i += 1
                ctype = strlib.ASCII
                continue

            new_notation += c
            i += 1

        return new_notation

    def match_len(self, string: str, pattern: str) -> int:
        """
        Returns the length of the substring that matches the patern
        from the beginning. The pattern must have been standardized.

        Parameters
        ----------
        string: str
            A long string starting with the pattern.
        pattern: str
            The search pattern.

        Returns
        -------
        int
            The length of the substring that matches the pattern.
            If it does not match exactly, it returns 0.
        """
        logger.debug("Searching {} in {}".format(pattern, string))
        pattern_pos = string_pos = 0
        while pattern_pos < len(pattern):
            if string_pos >= len(string):
                return 0

            c = pattern[pattern_pos]
            if c < '0' or c > '9':
                # Compare not numeric character
                logger.debug("Comparing '{}' with '{}'".format(
                    c, string[string_pos]))
                if string[string_pos] != c:
                    return 0

                pattern_pos += 1
                string_pos += 1
                continue

            # Compare numbers:
            # Check if the numeric sequence of the search string
            # matches the number expected by the pattern.
            period_pos = pattern.find('.', pattern_pos)
            if period_pos < 0:
                raise RuntimeError(
                    "No period after a number in the pattern string.")

            expected = int(pattern[pattern_pos:period_pos])
            logger.debug("Comparing string {} with expected value {}".format(
                string[string_pos:], expected))
            for span in range(string_pos + 1, len(string) + 1):
                if strlib.get_numeric_char(string[span - 1]) is False:
                    break

                string_num = string[string_pos:span]
                string_val = (strlib.get_number(string_num))['n']
                logger.debug("  substring {} is interpreted to {}".format(
                    string_num, string_val))
                if string_val == expected:
                    pattern_pos = period_pos + 1
                    string_pos = span
                    logger.debug("Substring {} matches".format(
                        string_num))
                    break

            if pattern_pos < period_pos:
                # The number did not match the expected value
                return 0

        return string_pos


# Create the singleton object of a converter
# that normalizes address strings
if 'converter' not in vars():
    converter = Converter()
