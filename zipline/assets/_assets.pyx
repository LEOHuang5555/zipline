# cython: embedsignature=True
#
# Copyright 2015 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Cythonized Asset object.
"""
cimport cython
from cpython.number cimport PyNumber_Index
from cpython.object cimport (
    Py_EQ,
    Py_NE,
    Py_GE,
    Py_LE,
    Py_GT,
    Py_LT,
)
from cpython cimport bool

import numpy as np
from numpy cimport int64_t
import warnings
cimport numpy as np

from trading_calendars import get_calendar


# Users don't construct instances of this object, and embedding the signature
# in the docstring seems to confuse Sphinx, so disable it for now.
@cython.embedsignature(False)
cdef class Asset:
    """
    Base class for entities that can be owned by a trading algorithm.

    Attributes
    ----------
    sid : int
        Internal Zipline sid. Persistent unique identifier assigned to the asset.
    zipline_sid: int
        Internal Zipline sid (alias for Asset.sid).
    real_sid: str
        The QuantRocket sid.
    symbol : str
        Most recent ticker under which the asset traded. This field can change
        without warning if the asset changes tickers. Use ``real_sid`` if you need a
        persistent identifier.
    asset_name : str
        Full name of the asset.
    exchange : str
        Canonical short name of the exchange on which the asset trades (e.g.,
        'NYSE').
    exchange_full : str
        Full name of the exchange on which the asset trades (e.g., 'NEW YORK
        STOCK EXCHANGE').
    country_code : str
        Two character code indicating the country in which the asset trades.
    currency : str
        ISO currency of asset.
    start_date : pd.Timestamp
        Date on which the asset first traded.
    end_date : pd.Timestamp
        Last date on which the asset traded. This value is set
        to the current (real time) date for assets that are still trading.
    tick_size : float
        Minimum amount that the price can change for this asset.
    multiplier : float
        The contract multiplier
    price_magnifier : float
        The price magnifier by which to divide prices when prices are quoted in a smaller
        unit than the asset's currency.
    auto_close_date : pd.Timestamp
        Date on which positions in this asset will be automatically liquidated
        to cash during a simulation. By default, this is three days after
        ``end_date``.
    """

    _kwargnames = frozenset({
        'sid',
        'real_sid',
        'symbol',
        'asset_name',
        'start_date',
        'end_date',
        'first_traded',
        'auto_close_date',
        'tick_size',
        'multiplier',
        'price_magnifier',
        'exchange_info',
        'currency',
    })

    def __init__(self,
                 int64_t sid, # sid is required
                 object real_sid, # real_sid is required
                 object exchange_info, # exchange is required
                 object currency, # currency is required
                 object symbol="",
                 object asset_name="",
                 object start_date=None,
                 object end_date=None,
                 object first_traded=None,
                 object auto_close_date=None,
                 object tick_size=0.01,
                 float multiplier=1.0,
                 float price_magnifier=1.0):

        self.sid = sid
        self.real_sid = real_sid
        self.symbol = symbol
        self.asset_name = asset_name
        self.exchange_info = exchange_info
        self.currency = currency
        self.start_date = start_date
        self.end_date = end_date
        self.first_traded = first_traded
        self.auto_close_date = auto_close_date
        self.tick_size = tick_size
        self.price_multiplier = multiplier
        self.price_magnifier = price_magnifier

    @property
    def zipline_sid(self):
        return self.sid

    @property
    def exchange(self):
        return self.exchange_info.canonical_name

    @property
    def exchange_full(self):
        return self.exchange_info.name

    @property
    def country_code(self):
        return self.exchange_info.country_code

    def __int__(self):
        return self.sid

    def __index__(self):
        return self.sid

    def __hash__(self):
        return self.sid

    def __richcmp__(x, y, int op):
        """
        Cython rich comparison method.  This is used in place of various
        equality checkers in pure python.
        """
        cdef int64_t x_as_int, y_as_int

        try:
            x_as_int = PyNumber_Index(x)
        except (TypeError, OverflowError):
            return NotImplemented

        try:
            y_as_int = PyNumber_Index(y)
        except (TypeError, OverflowError):
            return NotImplemented

        compared = x_as_int - y_as_int

        # Handle == and != first because they're significantly more common
        # operations.
        if op == Py_EQ:
            return compared == 0
        elif op == Py_NE:
            return compared != 0
        elif op == Py_LT:
            return compared < 0
        elif op == Py_LE:
            return compared <= 0
        elif op == Py_GT:
            return compared > 0
        elif op == Py_GE:
            return compared >= 0
        else:
            raise AssertionError('%d is not an operator' % op)

    def __repr__(self):
        if self.symbol:
            return '%s(%s [%s])' % (type(self).__name__, self.real_sid, self.symbol)
        else:
            return '%s(%s)' % (type(self).__name__, self.real_sid)

    cpdef __reduce__(self):
        """
        Function used by pickle to determine how to serialize/deserialize this
        class.  Should return a tuple whose first element is self.__class__,
        and whose second element is a tuple of all the attributes that should
        be serialized/deserialized during pickling.
        """
        return (self.__class__, (self.sid,
                                 self.real_sid,
                                 self.exchange_info,
                                 self.currency,
                                 self.symbol,
                                 self.asset_name,
                                 self.start_date,
                                 self.end_date,
                                 self.first_traded,
                                 self.auto_close_date,
                                 self.tick_size,
                                 self.price_multiplier,
                                 self.price_magnifier))

    cpdef to_dict(self):
        """Convert to a python dict containing all attributes of the asset.

        This is often useful for debugging.

        Returns
        -------
        as_dict : dict
        """
        return {
            'sid': self.sid,
            'real_sid': self.real_sid,
            'symbol': self.symbol,
            'currency': self.currency,
            'asset_name': self.asset_name,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'first_traded': self.first_traded,
            'auto_close_date': self.auto_close_date,
            'exchange': self.exchange,
            'exchange_full': self.exchange_full,
            'tick_size': self.tick_size,
            'multiplier': self.price_multiplier,
            'price_magnifier': self.price_magnifier,
            'exchange_info': self.exchange_info,
        }

    @classmethod
    def from_dict(cls, dict_):
        """
        Build an Asset instance from a dict.
        """
        return cls(**{k: v for k, v in dict_.items() if k in cls._kwargnames})

    def is_alive_for_session(self, session_label):
        """
        Returns whether the asset is alive at the given dt.

        Parameters
        ----------
        session_label: pd.Timestamp
            The desired session label to check. (midnight UTC)

        Returns
        -------
        boolean: whether the asset is alive at the given dt.
        """
        cdef int64_t ref_start
        cdef int64_t ref_end

        ref_start = self.start_date.value
        if ref_start > session_label.value:
            return False

        if not self.auto_close_date:
            return True

        return session_label.value <= self.auto_close_date.value

    def is_exchange_open(self, dt_minute):
        """
        Parameters
        ----------
        dt_minute: pd.Timestamp (UTC, tz-aware)
            The minute to check.

        Returns
        -------
        boolean: whether the asset's exchange is open at the given minute.
        """
        calendar = get_calendar(self.exchange)
        return calendar.is_open_on_minute(dt_minute)


@cython.embedsignature(False)
cdef class Equity(Asset):
    """
    Asset subclass representing partial ownership of a company, trust, or
    partnership.
    """
    pass

@cython.embedsignature(False)
cdef class Future(Asset):
    """Asset subclass representing ownership of a futures contract.

    See Also
    --------
    zipline.api.continuous_future - Create a specifier for a continuous contract.
    """
    _kwargnames = frozenset({
        'sid',
        'real_sid',
        'symbol',
        'root_symbol',
        'asset_name',
        'currency',
        'start_date',
        'end_date',
        'notice_date',
        'expiration_date',
        'auto_close_date',
        'rollover_date',
        'first_traded',
        'exchange_info',
        'tick_size',
        'multiplier',
        'price_magnifier',
    })

    def __init__(self,
                 int64_t sid, # sid is required
                 object real_sid, # real_sid is required
                 object exchange_info, # exchange is required
                 object currency, # currency is required
                 object symbol="",
                 object root_symbol="",
                 object asset_name="",
                 object start_date=None,
                 object end_date=None,
                 object notice_date=None,
                 object expiration_date=None,
                 object auto_close_date=None,
                 object rollover_date=None,
                 object first_traded=None,
                 object tick_size=0.001,
                 float multiplier=1.0,
                 float price_magnifier=1.0):

        super().__init__(
            sid,
            real_sid,
            exchange_info,
            symbol=symbol,
            currency=currency,
            asset_name=asset_name,
            start_date=start_date,
            end_date=end_date,
            first_traded=first_traded,
            auto_close_date=auto_close_date,
            tick_size=tick_size,
            multiplier=multiplier,
            price_magnifier=price_magnifier
        )
        self.root_symbol = root_symbol
        self.notice_date = notice_date
        self.expiration_date = expiration_date
        self.rollover_date = rollover_date

        if auto_close_date is None:
            if notice_date is None:
                self.auto_close_date = expiration_date
            elif expiration_date is None:
                self.auto_close_date = notice_date
            else:
                self.auto_close_date = min(notice_date, expiration_date)

        if rollover_date is None:
            self.rollover_date = self.auto_close_date

    cpdef __reduce__(self):
        """
        Function used by pickle to determine how to serialize/deserialize this
        class.  Should return a tuple whose first element is self.__class__,
        and whose second element is a tuple of all the attributes that should
        be serialized/deserialized during pickling.
        """
        return (self.__class__, (self.sid,
                                 self.real_sid,
                                 self.exchange_info,
                                 self.currency,
                                 self.symbol,
                                 self.root_symbol,
                                 self.asset_name,
                                 self.start_date,
                                 self.end_date,
                                 self.notice_date,
                                 self.expiration_date,
                                 self.auto_close_date,
                                 self.rollover_date,
                                 self.first_traded,
                                 self.tick_size,
                                 self.price_multiplier,
                                 self.price_magnifier))

    cpdef to_dict(self):
        """
        Convert to a python dict.
        """
        super_dict = super(Future, self).to_dict()
        super_dict['root_symbol'] = self.root_symbol
        super_dict['notice_date'] = self.notice_date
        super_dict['expiration_date'] = self.expiration_date
        super_dict['rollover_date'] = self.rollover_date
        return super_dict


def make_asset_array(int size, Asset asset):
    cdef np.ndarray out = np.empty([size], dtype=object)
    out.fill(asset)
    return out
