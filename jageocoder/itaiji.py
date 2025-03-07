import json
from logging import getLogger
import os
import re
from typing import Union, List

import jaconv

from jageocoder.address import AddressLevel
from jageocoder.strlib import strlib

logger = getLogger(__name__)


class Converter(object):

    # Optional postfixes for each address level
    re_optional_postfixes = {
        AddressLevel.CITY: re.compile(r'(市|区|町|村)$'),
        AddressLevel.WARD: re.compile(r'(区)$'),
        AddressLevel.OAZA: re.compile(r'(町|条|線|丁|丁目|番|号|番丁|番町)$'),
        AddressLevel.AZA: re.compile(r'(町|条|線|丁|丁目|区|番|号)$'),
        AddressLevel.BLOCK: re.compile(r'(番|番地|号|地)$'),
        AddressLevel.BLD: re.compile(r'(号|番地)$'),
    }

    optional_prefixes = ['字', '大字', '小字']
    optional_letters_in_middle = 'ケヶガツッノ字区町'
    optional_strings_in_middle = ['大字', '小字']

    re_optional_prefixes = re.compile(r'^({})'.format(
        '|'.join(optional_prefixes)))
    re_optional_strings_in_middle = re.compile(r'^({})'.format(
        '|'.join(list(optional_letters_in_middle) +
                 optional_strings_in_middle)))
    re_optional_aza = re.compile(
        r'([^0-9０-９]{1,5}?)[甲乙丙丁イロハニホヘ0-9０-９]')

    kana_letters = (strlib.HIRAGANA, strlib.KATAKANA)
    latin1_letters = (strlib.ASCII, strlib.NUMERIC, strlib.ALPHABET)

    def __init__(self, lookahead: bool = True):
        """
        Initialize the converter.

        Parameters
        ----------
        lookahead: bool, optional
            If True, it will look beyond the query string to find
            a matching string. This will reduce the processing speed.

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

        self.lookahead = lookahead

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
        m = self.re_optional_prefixes.match(notation)
        if m:
            return len(m.group(1))

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
        if level not in self.__class__.re_optional_postfixes:
            return 0

        m = self.re_optional_postfixes[level].search(notation)
        if m:
            return len(m.group(1))

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

        # Convert the notation according to the following rules.
        # 1. Variant characters with representative characters
        # 2. ZENKAKU characters with HANKAKU characters
        # 3. Lower case characters with capitalized characters
        # 4. HIRAGANA with KATAKANA
        # 5. Other exceptions
        notation = notation.translate(
            self.trans_itaiji).translate(
            self.trans_z2h).upper()
        notation = jaconv.hira2kata(notation)
        notation = notation.replace('通リ', '通')

        new_notation = ""
        i = 0

        while i < len(notation):
            c = notation[i]

            # Replace hyphen-like characters with '-'
            if strlib.is_hyphen(c):
                new_notation += '-'
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
        c = s = 'x'
        while pattern_pos < len(pattern):
            if string_pos >= len(string):
                return 0

            pre_c = c
            pre_s = s
            c = pattern[pattern_pos]
            s = string[string_pos]
            if c < '0' or c > '9':
                # Compare not numeric character
                logger.debug("Comparing '{}'({}) with '{}'({})".format(
                    c, pattern_pos, s, string_pos))
                if c != s:
                    if pre_s + s in self.optional_strings_in_middle:
                        logger.debug('"{}" in query "{}" is optional.'.format(
                            string[string_pos - 1: string_pos + 1],
                            string))
                        string_pos += 1
                        pattern_pos -= 1
                        continue

                    if pre_c + c in self.optional_strings_in_middle:
                        msg = '"{}" in pattern "{}" is optional.'
                        logger.debug(msg.format(
                            string[pattern_pos - 1: pattern_pos + 1],
                            pattern))
                        string_pos -= 1
                        pattern_pos += 1
                        continue

                    slen = self.optional_str_len(string, string_pos)
                    if slen > 0:
                        logger.debug('"{}" in query "{}" is optional.'.format(
                            string[string_pos: string_pos + slen], string))
                        string_pos += slen
                        continue

                    plen = self.optional_str_len(pattern, pattern_pos)
                    if plen > 0:
                        msg = '"{}" in pattern "{}" is optional.'
                        logger.debug(msg.format(
                            pattern[pattern_pos: pattern_pos + plen],
                            pattern))
                        pattern_pos += plen
                        continue

                    if self.lookahead is False:
                        return 0

                    azalen = self.optional_aza_len(string, string_pos)
                    if azalen > 0:
                        logger.debug('"{}" in query "{}" is optional.'.format(
                            string[string_pos: string_pos + azalen], string))
                        string_pos += azalen
                        continue

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

            slen = self.optional_str_len(string, string_pos)
            if slen > 0:
                logger.debug('"{}" in query "{}" is optional.'.format(
                    string[string_pos: string_pos + slen], string))
                string_pos += slen
                continue

            expected = int(pattern[pattern_pos:period_pos])
            logger.debug("Comparing string {} with expected value {}".format(
                string[string_pos:], expected))
            candidate = strlib.get_number(string[string_pos:], expected)
            if candidate['n'] == expected and candidate['i'] > 0:
                logger.debug("Substring {} matches".format(
                    string[string_pos: string_pos + candidate['i']]))
                pattern_pos = period_pos + 1
                string_pos += candidate['i']
            else:
                # The number did not match the expected value
                return 0

        return string_pos

    def optional_str_len(self, string: str, pos: int) -> int:
        m = self.re_optional_strings_in_middle.match(
            string[pos:])

        if m:
            return len(m.group(1))

        return 0

    def optional_aza_len(self, string: str, pos: int) -> int:
        m = self.re_optional_aza.match(string[pos:])
        if m:
            return len(m.group(1))

        return 0

    def standardized_candidates(
            self, string: str, from_pos: int = 0) -> List[str]:
        """
        Enumerate possible candidates for the notation after standardization.

        This method is called recursively.

        Parameters
        ----------
        string: str
            The original address notation.
        from_pos: int, optional
            The character indent from where the processing starts.

        Results
        -------
        A list of str
            A list of candidate strings.
        """
        candidates = [string]
        for pos in range(from_pos,
                         len(self.optional_letters_in_middle) +
                         len(self.optional_strings_in_middle)):
            if pos < len(self.optional_strings_in_middle):
                substr = self.optional_strings_in_middle[pos]
            else:
                substr = self.optional_letters_in_middle[
                    pos - len(self.optional_strings_in_middle)]

            if string.find(substr) >= 0:
                logger.debug('"{}" is in "{}"'.format(substr, string))
                candidates += self.standardized_candidates(
                    string.replace(substr, ''), pos + 1)

        return candidates


# Create the singleton object of a converter
# that normalizes address strings
if 'converter' not in vars():
    converter = Converter()
