"""
Tests for Factor terms.
"""
from functools import partial
from itertools import product
from parameterized import parameterized
from unittest import TestCase

from toolz import compose
import numpy as np
from numpy import (
    apply_along_axis,
    arange,
    array,
    datetime64,
    empty,
    eye,
    inf,
    log1p,
    nan,
    ones,
    ones_like,
    rot90,
    where,
)
from numpy.random import randn, seed
import pandas as pd
from scipy.stats.mstats import winsorize as scipy_winsorize

from zipline.errors import BadPercentileBounds, UnknownRankMethod
from zipline.lib.labelarray import LabelArray
from zipline.lib.rank import masked_rankdata_2d
from zipline.lib.normalize import naive_grouped_rowwise_apply as grouped_apply
from zipline.pipeline import Classifier, Factor, Filter, Pipeline
from zipline.pipeline.data import DataSet, Column, EquityPricing
from zipline.pipeline.factors import (
    CustomFactor,
    DailyReturns,
    Returns,
    PercentChange,
)
from zipline.pipeline.factors.factor import (
    summary_funcs,
    winsorize as zp_winsorize,
)
from zipline._testing import (
    check_allclose,
    check_arrays,
    parameter_space,
    permute_rows,
)
from zipline._testing.fixtures import (
    WithUSEquityPricingPipelineEngine,
    ZiplineTestCase,
)
from zipline._testing.predicates import assert_equal
from zipline.utils.numpy_utils import (
    as_column,
    categorical_dtype,
    datetime64ns_dtype,
    float64_dtype,
    ignore_nanwarnings,
    int64_dtype,
    NaTns,
)
from zipline.utils.math_utils import nanmean, nanstd

from .base import BaseUSEquityPipelineTestCase


class F(Factor):
    dtype = float64_dtype
    inputs = ()
    window_length = 0


class OtherF(Factor):
    dtype = float64_dtype
    inputs = ()
    window_length = 0


class C(Classifier):
    dtype = int64_dtype
    missing_value = -1
    inputs = ()
    window_length = 0


class OtherC(Classifier):
    dtype = int64_dtype
    missing_value = -1
    inputs = ()
    window_length = 0


class Mask(Filter):
    inputs = ()
    window_length = 0


for_each_factor_dtype = parameterized.expand([
    ('datetime64[ns]', datetime64ns_dtype),
    ('float', float64_dtype),
])


def scipy_winsorize_with_nan_handling(array, limits):
    """
    Wrapper around scipy.stats.mstats.winsorize that handles NaNs correctly.

    scipy's winsorize sorts NaNs to the end of the array when calculating
    percentiles.
    """
    # The basic idea of this function is to do the following:
    # 1. Sort the input, sorting nans to the end of the array.
    # 2. Call scipy winsorize on the non-nan portion of the input.
    # 3. Undo the sorting to put the winsorized values back in their original
    #    locations.

    nancount = np.isnan(array).sum()
    if nancount == len(array):
        return array.copy()

    sorter = array.argsort()
    unsorter = sorter.argsort()  # argsorting a permutation gives its inverse!

    if nancount:
        sorted_non_nans = array[sorter][:-nancount]
    else:
        sorted_non_nans = array[sorter]

    sorted_winsorized = np.hstack([
        scipy_winsorize(sorted_non_nans, limits).data,
        np.full(nancount, np.nan),
    ])

    return sorted_winsorized[unsorter]


class FactorTestCase(BaseUSEquityPipelineTestCase):

    def init_instance_fixtures(self):
        super(FactorTestCase, self).init_instance_fixtures()
        self.f = F()

    def test_bad_input(self):
        with self.assertRaises(UnknownRankMethod):
            self.f.rank("not a real rank method")

    @parameter_space(method_name=['isnan', 'notnan', 'isfinite'])
    def test_float64_only_ops(self, method_name):
        class NotFloat(Factor):
            dtype = datetime64ns_dtype
            inputs = ()
            window_length = 0

        nf = NotFloat()
        meth = getattr(nf, method_name)
        with self.assertRaises(TypeError):
            meth()

    @parameter_space(custom_missing_value=[-1, 0])
    def test_isnull_int_dtype(self, custom_missing_value):

        class CustomMissingValue(Factor):
            dtype = int64_dtype
            window_length = 0
            missing_value = custom_missing_value
            inputs = ()

        factor = CustomMissingValue()

        data = arange(25).reshape(5, 5)
        data[eye(5, dtype=bool)] = custom_missing_value

        self.check_terms(
            {
                'isnull': factor.isnull(),
                'notnull': factor.notnull(),
            },
            {
                'isnull': eye(5, dtype=bool),
                'notnull': ~eye(5, dtype=bool),
            },
            initial_workspace={factor: data},
            mask=self.build_mask(ones((5, 5))),
        )

    def test_isnull_datetime_dtype(self):
        class DatetimeFactor(Factor):
            dtype = datetime64ns_dtype
            window_length = 0
            inputs = ()

        factor = DatetimeFactor()

        data = arange(25).reshape(5, 5).astype('datetime64[ns]')
        data[eye(5, dtype=bool)] = NaTns

        self.check_terms(
            {
                'isnull': factor.isnull(),
                'notnull': factor.notnull(),
            },
            {
                'isnull': eye(5, dtype=bool),
                'notnull': ~eye(5, dtype=bool),
            },
            initial_workspace={factor: data},
            mask=self.build_mask(ones((5, 5))),
        )

    @for_each_factor_dtype
    def test_rank_ascending(self, name, factor_dtype):

        f = F(dtype=factor_dtype)

        # Generated with:
        # data = arange(25).reshape(5, 5).transpose() % 4
        data = array([[0, 1, 2, 3, 0],
                      [1, 2, 3, 0, 1],
                      [2, 3, 0, 1, 2],
                      [3, 0, 1, 2, 3],
                      [0, 1, 2, 3, 0]], dtype=factor_dtype)

        expected_ranks = {
            'ordinal': array([[1., 3., 4., 5., 2.],
                              [2., 4., 5., 1., 3.],
                              [3., 5., 1., 2., 4.],
                              [4., 1., 2., 3., 5.],
                              [1., 3., 4., 5., 2.]]),
            'average': array([[1.5, 3., 4., 5., 1.5],
                              [2.5, 4., 5., 1., 2.5],
                              [3.5, 5., 1., 2., 3.5],
                              [4.5, 1., 2., 3., 4.5],
                              [1.5, 3., 4., 5., 1.5]]),
            'min': array([[1., 3., 4., 5., 1.],
                          [2., 4., 5., 1., 2.],
                          [3., 5., 1., 2., 3.],
                          [4., 1., 2., 3., 4.],
                          [1., 3., 4., 5., 1.]]),
            'max': array([[2., 3., 4., 5., 2.],
                          [3., 4., 5., 1., 3.],
                          [4., 5., 1., 2., 4.],
                          [5., 1., 2., 3., 5.],
                          [2., 3., 4., 5., 2.]]),
            'dense': array([[1., 2., 3., 4., 1.],
                            [2., 3., 4., 1., 2.],
                            [3., 4., 1., 2., 3.],
                            [4., 1., 2., 3., 4.],
                            [1., 2., 3., 4., 1.]]),
        }

        def check(terms):
            self.check_terms(
                terms,
                expected={name: expected_ranks[name] for name in terms},
                initial_workspace={f: data},
                mask=self.build_mask(ones((5, 5))),
            )

        check({meth: f.rank(method=meth) for meth in expected_ranks})
        check({
            meth: f.rank(method=meth, ascending=True)
            for meth in expected_ranks
        })
        # Not passing a method should default to ordinal.
        check({'ordinal': f.rank()})
        check({'ordinal': f.rank(ascending=True)})

    @for_each_factor_dtype
    def test_rank_descending(self, name, factor_dtype):

        f = F(dtype=factor_dtype)

        # Generated with:
        # data = arange(25).reshape(5, 5).transpose() % 4
        data = array([[0, 1, 2, 3, 0],
                      [1, 2, 3, 0, 1],
                      [2, 3, 0, 1, 2],
                      [3, 0, 1, 2, 3],
                      [0, 1, 2, 3, 0]], dtype=factor_dtype)
        expected_ranks = {
            'ordinal': array([[4., 3., 2., 1., 5.],
                              [3., 2., 1., 5., 4.],
                              [2., 1., 5., 4., 3.],
                              [1., 5., 4., 3., 2.],
                              [4., 3., 2., 1., 5.]]),
            'average': array([[4.5, 3., 2., 1., 4.5],
                              [3.5, 2., 1., 5., 3.5],
                              [2.5, 1., 5., 4., 2.5],
                              [1.5, 5., 4., 3., 1.5],
                              [4.5, 3., 2., 1., 4.5]]),
            'min': array([[4., 3., 2., 1., 4.],
                          [3., 2., 1., 5., 3.],
                          [2., 1., 5., 4., 2.],
                          [1., 5., 4., 3., 1.],
                          [4., 3., 2., 1., 4.]]),
            'max': array([[5., 3., 2., 1., 5.],
                          [4., 2., 1., 5., 4.],
                          [3., 1., 5., 4., 3.],
                          [2., 5., 4., 3., 2.],
                          [5., 3., 2., 1., 5.]]),
            'dense': array([[4., 3., 2., 1., 4.],
                            [3., 2., 1., 4., 3.],
                            [2., 1., 4., 3., 2.],
                            [1., 4., 3., 2., 1.],
                            [4., 3., 2., 1., 4.]]),
        }

        def check(terms):
            self.check_terms(
                terms,
                expected={name: expected_ranks[name] for name in terms},
                initial_workspace={f: data},
                mask=self.build_mask(ones((5, 5))),
            )

        check({
            meth: f.rank(method=meth, ascending=False)
            for meth in expected_ranks
        })
        # Not passing a method should default to ordinal.
        check({'ordinal': f.rank(ascending=False)})

    @for_each_factor_dtype
    def test_rank_after_mask(self, name, factor_dtype):

        f = F(dtype=factor_dtype)
        # data = arange(25).reshape(5, 5).transpose() % 4
        data = array([[0, 1, 2, 3, 0],
                      [1, 2, 3, 0, 1],
                      [2, 3, 0, 1, 2],
                      [3, 0, 1, 2, 3],
                      [0, 1, 2, 3, 0]], dtype=factor_dtype)
        mask_data = ~eye(5, dtype=bool)
        initial_workspace = {f: data, Mask(): mask_data}

        terms = {
            "ascending_nomask": f.rank(ascending=True),
            "ascending_mask": f.rank(ascending=True, mask=Mask()),
            "descending_nomask": f.rank(ascending=False),
            "descending_mask": f.rank(ascending=False, mask=Mask()),
        }

        expected = {
            "ascending_nomask": array([[1., 3., 4., 5., 2.],
                                       [2., 4., 5., 1., 3.],
                                       [3., 5., 1., 2., 4.],
                                       [4., 1., 2., 3., 5.],
                                       [1., 3., 4., 5., 2.]]),
            "descending_nomask": array([[4., 3., 2., 1., 5.],
                                        [3., 2., 1., 5., 4.],
                                        [2., 1., 5., 4., 3.],
                                        [1., 5., 4., 3., 2.],
                                        [4., 3., 2., 1., 5.]]),
            # Diagonal should be all nans, and anything whose rank was less
            # than the diagonal in the unmasked calc should go down by 1.
            "ascending_mask": array([[nan, 2., 3., 4., 1.],
                                     [2., nan, 4., 1., 3.],
                                     [2., 4., nan, 1., 3.],
                                     [3., 1., 2., nan, 4.],
                                     [1., 2., 3., 4., nan]]),
            "descending_mask": array([[nan, 3., 2., 1., 4.],
                                      [2., nan, 1., 4., 3.],
                                      [2., 1., nan, 4., 3.],
                                      [1., 4., 3., nan, 2.],
                                      [4., 3., 2., 1., nan]]),
        }

        self.check_terms(
            terms,
            expected,
            initial_workspace,
            mask=self.build_mask(ones((5, 5))),
        )

    @for_each_factor_dtype
    def test_grouped_rank_ascending(self, name, factor_dtype=float64_dtype):

        f = F(dtype=factor_dtype)
        class StrC(C):
            dtype = categorical_dtype
            missing_value = None

        c = C()
        str_c = StrC()

        # Generated with:
        # data = arange(25).reshape(5, 5).transpose() % 4
        data = array([[0, 1, 2, 3, 0],
                      [1, 2, 3, 0, 1],
                      [2, 3, 0, 1, 2],
                      [3, 0, 1, 2, 3],
                      [0, 1, 2, 3, 0]], dtype=factor_dtype)

        # Generated with:
        # classifier_data = arange(25).reshape(5, 5).transpose() % 2
        classifier_data = array([[0, 1, 0, 1, 0],
                                 [1, 0, 1, 0, 1],
                                 [0, 1, 0, 1, 0],
                                 [1, 0, 1, 0, 1],
                                 [0, 1, 0, 1, 0]], dtype=int64_dtype)
        string_classifier_data = LabelArray(
            classifier_data.astype(str).astype(object),
            missing_value=None,
        )

        expected_ranks = {
            'ordinal': array(
                [[1., 1., 3., 2., 2.],
                 [1., 2., 3., 1., 2.],
                 [2., 2., 1., 1., 3.],
                 [2., 1., 1., 2., 3.],
                 [1., 1., 3., 2., 2.]]
            ),
            'average': array(
                [[1.5, 1., 3., 2., 1.5],
                 [1.5, 2., 3., 1., 1.5],
                 [2.5, 2., 1., 1., 2.5],
                 [2.5, 1., 1., 2., 2.5],
                 [1.5, 1., 3., 2., 1.5]]
            ),
            'min': array(
                [[1., 1., 3., 2., 1.],
                 [1., 2., 3., 1., 1.],
                 [2., 2., 1., 1., 2.],
                 [2., 1., 1., 2., 2.],
                 [1., 1., 3., 2., 1.]]
            ),
            'max': array(
                [[2., 1., 3., 2., 2.],
                 [2., 2., 3., 1., 2.],
                 [3., 2., 1., 1., 3.],
                 [3., 1., 1., 2., 3.],
                 [2., 1., 3., 2., 2.]]
            ),
            'dense': array(
                [[1., 1., 2., 2., 1.],
                 [1., 2., 2., 1., 1.],
                 [2., 2., 1., 1., 2.],
                 [2., 1., 1., 2., 2.],
                 [1., 1., 2., 2., 1.]]
            ),
        }

        def check(terms):
            self.check_terms(
                terms,
                expected={name: expected_ranks[name] for name in terms},
                initial_workspace={
                    f: data,
                    c: classifier_data,
                    str_c: string_classifier_data,
                },
                mask=self.build_mask(ones((5, 5))),
            )

        # Not specifying the value of ascending param should default to True
        check({
            meth: f.rank(method=meth, groupby=c)
            for meth in expected_ranks
        })
        check({
            meth: f.rank(method=meth, groupby=str_c)
            for meth in expected_ranks
        })
        check({
            meth: f.rank(method=meth, groupby=c, ascending=True)
            for meth in expected_ranks
        })
        check({
            meth: f.rank(method=meth, groupby=str_c, ascending=True)
            for meth in expected_ranks
        })

        # Not passing a method should default to ordinal
        check({'ordinal': f.rank(groupby=c)})
        check({'ordinal': f.rank(groupby=str_c)})
        check({'ordinal': f.rank(groupby=c, ascending=True)})
        check({'ordinal': f.rank(groupby=str_c, ascending=True)})

    @for_each_factor_dtype
    def test_grouped_rank_descending(self, name, factor_dtype):

        f = F(dtype=factor_dtype)
        class StrC(C):
            dtype = categorical_dtype
            missing_value = None

        c = C()
        str_c = StrC()

        # Generated with:
        # data = arange(25).reshape(5, 5).transpose() % 4
        data = array([[0, 1, 2, 3, 0],
                      [1, 2, 3, 0, 1],
                      [2, 3, 0, 1, 2],
                      [3, 0, 1, 2, 3],
                      [0, 1, 2, 3, 0]], dtype=factor_dtype)

        # Generated with:
        # classifier_data = arange(25).reshape(5, 5).transpose() % 2
        classifier_data = array([[0, 1, 0, 1, 0],
                                 [1, 0, 1, 0, 1],
                                 [0, 1, 0, 1, 0],
                                 [1, 0, 1, 0, 1],
                                 [0, 1, 0, 1, 0]], dtype=int64_dtype)

        string_classifier_data = LabelArray(
            classifier_data.astype(str).astype(object),
            missing_value=None,
        )

        expected_ranks = {
            'ordinal': array(
                [[2., 2., 1., 1., 3.],
                 [2., 1., 1., 2., 3.],
                 [1., 1., 3., 2., 2.],
                 [1., 2., 3., 1., 2.],
                 [2., 2., 1., 1., 3.]]
            ),
            'average': array(
                [[2.5, 2., 1., 1., 2.5],
                 [2.5, 1., 1., 2., 2.5],
                 [1.5, 1., 3., 2., 1.5],
                 [1.5, 2., 3., 1., 1.5],
                 [2.5, 2., 1., 1., 2.5]]
            ),
            'min': array(
                [[2., 2., 1., 1., 2.],
                 [2., 1., 1., 2., 2.],
                 [1., 1., 3., 2., 1.],
                 [1., 2., 3., 1., 1.],
                 [2., 2., 1., 1., 2.]]
            ),
            'max': array(
                [[3., 2., 1., 1., 3.],
                 [3., 1., 1., 2., 3.],
                 [2., 1., 3., 2., 2.],
                 [2., 2., 3., 1., 2.],
                 [3., 2., 1., 1., 3.]]
            ),
            'dense': array(
                [[2., 2., 1., 1., 2.],
                 [2., 1., 1., 2., 2.],
                 [1., 1., 2., 2., 1.],
                 [1., 2., 2., 1., 1.],
                 [2., 2., 1., 1., 2.]]
            ),
        }

        def check(terms):
            self.check_terms(
                terms,
                expected={name: expected_ranks[name] for name in terms},
                initial_workspace={
                    f: data,
                    c: classifier_data,
                    str_c: string_classifier_data,
                },
                mask=self.build_mask(ones((5, 5))),
            )

        check({
            meth: f.rank(method=meth, groupby=c, ascending=False)
            for meth in expected_ranks
        })
        check({
            meth: f.rank(method=meth, groupby=str_c, ascending=False)
            for meth in expected_ranks
        })

        # Not passing a method should default to ordinal
        check({'ordinal': f.rank(groupby=c, ascending=False)})
        check({'ordinal': f.rank(groupby=str_c, ascending=False)})

    def test_returns_when_exclude_window_length_too_long(self):

        with self.assertRaises(ValueError) as cm:
            returns = Returns(window_length=10, exclude_window_length=10)

        self.assertIn(
            'window_length must be greater than exclude_window_length',
            str(cm.exception))

    @parameterized.expand([
        (100, 15, None),
        (101, 4, 0),
        (102, 100, None),
        (102, 100, 20),
        ])
    def test_returns(self, seed_value, window_length, exclude_window_length):

        kwargs = {"window_length": window_length}
        if exclude_window_length is not None:
            kwargs["exclude_window_length"] = exclude_window_length
        returns = Returns(**kwargs)

        today = datetime64(1, 'ns')
        assets = arange(3)

        seed(seed_value)  # Seed so we get deterministic results.
        test_data = abs(randn(window_length, 3))

        # Calculate the expected returns
        expected = (test_data[-1 - (exclude_window_length or 0)] - test_data[0]) / test_data[0]

        out = empty((3,), dtype=float)
        returns.compute(today, assets, out, test_data, returns.params["exclude_window_length"])

        check_allclose(expected, out)

    @parameterized.expand([
        (100, 15),
        (101, 4),
        (102, 100),
        ])
    def test_percentchange(self, seed_value, window_length):

        pct_change = PercentChange(
            inputs=[EquityPricing.close],
            window_length=window_length,
        )

        today = datetime64(1, 'ns')
        assets = arange(8)

        seed(seed_value)  # Seed so we get deterministic results.
        middle_rows = randn(window_length - 2, 8)
        first_row = array([1, 2, 2, 1, -1, -1, 0, nan])
        end_row = array([2, 1, 2, -2, 2, -2, 1, 1])
        test_data = np.vstack([first_row, middle_rows, end_row])

        # Calculate the expected percent change
        expected = array([1, -0.5, 0, -3, 3, -1, inf, nan])

        out = empty((8,), dtype=float)
        pct_change.compute(today, assets, out, test_data)

        check_allclose(expected, out)

        # repeat using the pct_change method
        pct_change = EquityPricing.close.latest.pct_change(window_length+1)
        out = empty((8,), dtype=float)
        pct_change.compute(today, assets, out, test_data)

        with self.assertRaises(ValueError):
            PercentChange(inputs=(), window_length=2)

        with self.assertRaises(ValueError):
            PercentChange(inputs=[EquityPricing.close], window_length=1)

    def test_scalar_inputs(self):
        """
        Tests the inputs can be an iterable or a scalar.
        """

        pct_change = PercentChange(
            inputs=[EquityPricing.close],
            window_length=2,
        )
        self.assertEqual(
            pct_change.inputs, (EquityPricing.close,))

        pct_change = PercentChange(
            inputs=EquityPricing.open,
            window_length=2,
        )

        self.assertEqual(
            pct_change.inputs, (EquityPricing.open,))

    def gen_ranking_cases():
        seeds = range(int(1e4), int(1e5), int(1e4))
        methods = ('ordinal', 'average')
        use_mask_values = (True, False)
        set_missing_values = (True, False)
        ascending_values = (True, False)
        return product(
            seeds,
            methods,
            use_mask_values,
            set_missing_values,
            ascending_values,
        )

    @parameterized.expand(gen_ranking_cases())
    def test_masked_rankdata_2d(self,
                                seed_value,
                                method,
                                use_mask,
                                set_missing,
                                ascending):
        eyemask = ~eye(5, dtype=bool)
        nomask = ones((5, 5), dtype=bool)

        seed(seed_value)
        asfloat = (randn(5, 5) * seed_value)
        asdatetime = (asfloat).copy().view('datetime64[ns]')

        mask = eyemask if use_mask else nomask
        if set_missing:
            asfloat[:, 2] = nan
            asdatetime[:, 2] = NaTns

        float_result = masked_rankdata_2d(
            data=asfloat,
            mask=mask,
            missing_value=nan,
            method=method,
            ascending=True,
        )
        datetime_result = masked_rankdata_2d(
            data=asdatetime,
            mask=mask,
            missing_value=NaTns,
            method=method,
            ascending=True,
        )

        check_arrays(float_result, datetime_result)

    def test_normalizations_hand_computed(self):
        """
        Test the hand-computed example in factor.demean.
        """
        f = self.f
        m = Mask()

        class StrC(C):
            dtype = categorical_dtype
            missing_value = None

        c = C()
        str_c = StrC()

        factor_data = array(
            [[1.0, 2.0, 3.0, 4.0],
             [1.5, 2.5, 3.5, 1.0],
             [2.0, 3.0, 4.0, 1.5],
             [2.5, 3.5, 1.0, 2.0]],
        )
        filter_data = array(
            [[False, True, True, True],
             [True, False, True, True],
             [True, True, False, True],
             [True, True, True, False]],
            dtype=bool,
        )
        classifier_data = array(
            [[1, 1, 2, 2],
             [1, 1, 2, 2],
             [1, 1, 2, 2],
             [1, 1, 2, 2]],
            dtype=int64_dtype,
        )
        string_classifier_data = LabelArray(
            classifier_data.astype(str).astype(object),
            missing_value=None,
        )

        terms = {
            'vanilla': f.demean(),
            'masked': f.demean(mask=m),
            'grouped': f.demean(groupby=c),
            'grouped_str': f.demean(groupby=str_c),
            'grouped_masked': f.demean(mask=m, groupby=c),
            'grouped_masked_str': f.demean(mask=m, groupby=str_c),
        }
        expected = {
            'vanilla': array(
                [[-1.500, -0.500,  0.500,  1.500],
                 [-0.625,  0.375,  1.375, -1.125],
                 [-0.625,  0.375,  1.375, -1.125],
                 [0.250,   1.250, -1.250, -0.250]],
            ),
            'masked': array(
                [[nan,    -1.000,  0.000,  1.000],
                 [-0.500,    nan,  1.500, -1.000],
                 [-0.166,  0.833,    nan, -0.666],
                 [0.166,   1.166, -1.333,    nan]],
            ),
            'grouped': array(
                [[-0.500, 0.500, -0.500,  0.500],
                 [-0.500, 0.500,  1.250, -1.250],
                 [-0.500, 0.500,  1.250, -1.250],
                 [-0.500, 0.500, -0.500,  0.500]],
            ),
            'grouped_masked': array(
                [[nan,     0.000, -0.500,  0.500],
                 [0.000,     nan,  1.250, -1.250],
                 [-0.500,  0.500,    nan,  0.000],
                 [-0.500,  0.500,  0.000,    nan]]
            ),
        }
        # Changing the classifier dtype shouldn't affect anything.
        expected['grouped_str'] = expected['grouped']
        expected['grouped_masked_str'] = expected['grouped_masked']

        self.check_terms(
            terms,
            expected,
            initial_workspace={
                f: factor_data,
                c: classifier_data,
                str_c: string_classifier_data,
                m: filter_data,
            },
            mask=self.build_mask(self.ones_mask(shape=factor_data.shape)),
            # The hand-computed values aren't very precise (in particular,
            # we truncate repeating decimals at 3 places) This is just
            # asserting that the example isn't misleading by being totally
            # wrong.
            check=partial(check_allclose, atol=0.001),
        )

    def test_winsorize_hand_computed(self):
        """
        Test the hand-computed example in factor.winsorize.
        """
        f = self.f
        m = Mask()
        class StrC(C):
            dtype = categorical_dtype
            missing_value = None
        c = C()
        str_c = StrC()

        factor_data = array([
            [1.,     2.,  3.,  4.,   5.,   6.,  7.,  8.,  9.],
            [1.,     2.,  3.,  4.,   5.,   6., nan, nan, nan],
            [1.,     8., 27., 64., 125., 216., nan, nan, nan],
            [6.,     5.,  4.,  3.,   2.,   1., nan, nan, nan],
            [nan,   nan, nan, nan,  nan,  nan, nan, nan, nan],
        ])
        filter_data = array(
            [[1, 1, 1, 1, 1, 1, 1, 1, 1],
             [0, 1, 1, 1, 1, 1, 1, 1, 1],
             [1, 0, 1, 1, 1, 1, 1, 1, 1],
             [1, 1, 0, 1, 1, 1, 1, 1, 1],
             [1, 1, 1, 0, 1, 1, 1, 1, 1]],
            dtype=bool,
        )
        classifier_data = array(
            [[1, 1, 1, 2, 2, 2, 1, 1, 1],
             [1, 1, 1, 2, 2, 2, 1, 1, 1],
             [1, 1, 1, 2, 2, 2, 1, 1, 1],
             [1, 1, 1, 2, 2, 2, 1, 1, 1],
             [1, 1, 1, 2, 2, 2, 1, 1, 1]],
            dtype=int64_dtype,
        )
        string_classifier_data = LabelArray(
            classifier_data.astype(str).astype(object),
            missing_value=None,
        )

        terms = {
            'winsor_1': f.winsorize(
                min_percentile=0.33,
                max_percentile=0.67
            ),
            'winsor_2': f.winsorize(
                min_percentile=0.49,
                max_percentile=1
            ),
            'winsor_3': f.winsorize(
                min_percentile=0,
                max_percentile=.67
            ),
            'masked': f.winsorize(
                min_percentile=0.33,
                max_percentile=0.67,
                mask=m
            ),
            'grouped': f.winsorize(
                min_percentile=0.34,
                max_percentile=0.66,
                groupby=c
            ),
            'grouped_str': f.winsorize(
                min_percentile=0.34,
                max_percentile=0.66,
                groupby=str_c
            ),
            'grouped_masked': f.winsorize(
                min_percentile=0.34,
                max_percentile=0.66,
                mask=m,
                groupby=c
            ),
            'grouped_masked_str': f.winsorize(
                min_percentile=0.34,
                max_percentile=0.66,
                mask=m,
                groupby=str_c
            ),
        }
        expected = {
            'winsor_1': array([
                [3.,    3.,    3.,    4.,    5.,    6.,  7.,  7.,  7.],
                [2.,    2.,    3.,    4.,    5.,    5., nan, nan, nan],
                [8.,    8.,   27.,   64.,  125.,  125., nan, nan, nan],
                [5.,    5.,    4.,    3.,    2.,    2., nan, nan, nan],
                [nan,  nan,   nan,   nan,   nan,   nan, nan, nan, nan],
            ]),
            'winsor_2': array([
                [5.,     5.,    5.,    5.,    5.,    6.,  7.,  8.,  9.],
                [3.0,    3.,    3.,    4.,    5.,    6., nan, nan, nan],
                [27.,   27.,   27.,   64.,  125.,  216., nan, nan, nan],
                [6.0,    5.,    4.,    3.,    3.,    3., nan, nan, nan],
                [nan,   nan,   nan,   nan,   nan,   nan, nan, nan, nan],
            ]),
            'winsor_3': array([
                [1.,    2.,    3.,    4.,    5.,    6.,  7.,  7.,  7.],
                [1.,    2.,    3.,    4.,    5.,    5., nan, nan, nan],
                [1.,    8.,   27.,   64.,  125.,  125., nan, nan, nan],
                [5.,    5.,    4.,    3.,    2.,    1., nan, nan, nan],
                [nan,  nan,   nan,   nan,   nan,   nan, nan, nan, nan],
            ]),
            'masked': array([
                # no mask on first row
                [3.,     3.,    3.,    4.,    5.,    6.,  7.,  7.,  7.],
                [nan,    3.,    3.,    4.,    5.,    5., nan, nan, nan],
                [27.,   nan,   27.,   64.,  125.,  125., nan, nan, nan],
                [5.0,    5.,    nan,   3.,    2.,    2., nan, nan, nan],
                [nan,   nan,   nan,   nan,   nan,   nan, nan, nan, nan],
            ]),
            'grouped': array([
                [3.,    3.,    3.,    5.,    5.,    5.,  7.,  7.,  7.],
                [2.,    2.,    2.,    5.,    5.,    5., nan, nan, nan],
                [8.,    8.,    8.,  125.,  125.,  125., nan, nan, nan],
                [5.,    5.,    5.,    2.,    2.,    2., nan, nan, nan],
                [nan,  nan,   nan,   nan,   nan,   nan, nan, nan, nan],
            ]),
            'grouped_masked': array([
                [3.,     3.,    3.,    5.,    5.,    5.,  7.,  7.,  7.],
                [nan,    2.,    3.,    5.,    5.,    5., nan, nan, nan],
                [1.0,   nan,   27.,  125.,  125.,  125., nan, nan, nan],
                [6.0,    5.,   nan,    2.,    2.,    2., nan, nan, nan],
                [nan,   nan,   nan,   nan,   nan,   nan, nan, nan, nan],
            ]),
        }
        # Changing the classifier dtype shouldn't affect anything.
        expected['grouped_str'] = expected['grouped']
        expected['grouped_masked_str'] = expected['grouped_masked']

        self.check_terms(
            terms,
            expected,
            initial_workspace={
                f: factor_data,
                c: classifier_data,
                str_c: string_classifier_data,
                m: filter_data,
            },
            mask=self.build_mask(self.ones_mask(shape=factor_data.shape)),
            check=partial(check_allclose, atol=0.001),
        )

    def test_winsorize_no_nans(self):
        data = array([0., 1., 2., 3., 4., 5., 6., 7., 8., 9.])
        permutation = array([2, 1, 6, 8, 7, 5, 3, 9, 4, 0])

        for perm in slice(None), permutation:
            # Winsorize both tails at 90%.
            result = zp_winsorize(data[perm], 0.1, 0.9)
            expected = array([1., 1., 2., 3., 4., 5., 6., 7., 8., 8.])[perm]
            assert_equal(result, expected)

            # Winsorize both tails at 80%.
            result = zp_winsorize(data[perm], 0.2, 0.8)
            expected = array([2., 2., 2., 3., 4., 5., 6., 7., 7., 7.])[perm]
            assert_equal(result, expected)

            # Winsorize just the upper tail.
            result = zp_winsorize(data[perm], 0.0, 0.8)
            expected = array([0., 1., 2., 3., 4., 5., 6., 7., 7., 7.])[perm]
            assert_equal(result, expected)

            # Winsorize just the lower tail.
            result = zp_winsorize(data[perm], 0.2, 1.0)
            expected = array([2., 2., 2., 3., 4., 5., 6., 7., 8., 9.])[perm]
            assert_equal(result, expected)

            # Don't winsorize.
            result = zp_winsorize(data[perm], 0.0, 1.0)
            expected = array([0., 1., 2., 3., 4., 5., 6., 7., 8., 9.])[perm]
            assert_equal(result, expected)

    def test_winsorize_nans(self):
        # 5 low non-nan values, then some nans, then 5 high non-nans.
        data = array([4.0, 3.0, 0.0, 1.0, 2.0,
                      nan, nan, nan,
                      9.0, 5.0, 6.0, 8.0, 7.0])

        # Winsorize both tails at 10%.
        # 0.0 -> 1.0
        # 9.0 -> 8.0
        result = zp_winsorize(data, 0.10, 0.90)
        expected = array([4.0, 3.0, 1.0, 1.0, 2.0,
                          nan, nan, nan,
                          8.0, 5.0, 6.0, 8.0, 7.0])
        assert_equal(result, expected)

        # Winsorize both tails at 20%.
        # 0.0 and 1.0 -> 2.0
        # 9.0 and 8.0 -> 7.0
        result = zp_winsorize(data, 0.20, 0.80)
        expected = array([4.0, 3.0, 2.0, 2.0, 2.0,
                          nan, nan, nan,
                          7.0, 5.0, 6.0, 7.0, 7.0])
        assert_equal(result, expected)

        # Winsorize just the upper tail.
        result = zp_winsorize(data, 0, 0.8)
        expected = array([4.0, 3.0, 0.0, 1.0, 2.0,
                          nan, nan, nan,
                          7.0, 5.0, 6.0, 7.0, 7.0])
        assert_equal(result, expected)

        # Winsorize just the lower tail.
        result = zp_winsorize(data, 0.2, 1.0)
        expected = array([4.0, 3.0, 2.0, 2.0, 2.0,
                          nan, nan, nan,
                          9.0, 5.0, 6.0, 8.0, 7.0])
        assert_equal(result, expected)

    def test_winsorize_bad_bounds(self):
        """
        Test out of bounds input for factor.winsorize.
        """
        f = self.f

        bad_percentiles = [
            (-.1, 1),
            (0, 95),
            (5, 95),
            (5, 5),
            (.6, .4)
        ]
        for min_, max_ in bad_percentiles:
            with self.assertRaises(BadPercentileBounds):
                f.winsorize(min_percentile=min_, max_percentile=max_)

    @parameter_space(
        seed_value=[1, 2],
        normalizer_name_and_func=[
            ('demean', {}, lambda row: row - nanmean(row)),
            ('zscore', {}, lambda row: (row - nanmean(row)) / nanstd(row)),
            (
                'winsorize',
                {"min_percentile": 0.25, "max_percentile": 0.75},
                lambda row: scipy_winsorize_with_nan_handling(
                    row,
                    limits=0.25,
                )
            ),
        ],
        add_nulls_to_factor=(False, True,),
    )
    def test_normalizations_randomized(self,
                                       seed_value,
                                       normalizer_name_and_func,
                                       add_nulls_to_factor):

        name, kwargs, func = normalizer_name_and_func

        shape = (20, 20)

        # All Trues.
        nomask = self.ones_mask(shape=shape)
        # Falses on main diagonal.
        eyemask = self.eye_mask(shape=shape)
        # Falses on other diagonal.
        eyemask90 = rot90(eyemask)
        # Falses on both diagonals.
        xmask = eyemask & eyemask90

        # Block of random data.
        factor_data = self.randn_data(seed=seed_value, shape=shape)
        if add_nulls_to_factor:
            factor_data = where(eyemask, factor_data, nan)

        # Cycles of 0, 1, 2, 0, 1, 2, ...
        classifier_data = (
            (self.arange_data(shape=shape, dtype=int64_dtype) + seed_value) % 3
        )
        # With -1s on main diagonal.
        classifier_data_eyenulls = where(eyemask, classifier_data, -1)
        # With -1s on opposite diagonal.
        classifier_data_eyenulls90 = where(eyemask90, classifier_data, -1)
        # With -1s on both diagonals.
        classifier_data_xnulls = where(xmask, classifier_data, -1)

        f = self.f
        c = C()
        c_with_nulls = OtherC()
        m = Mask()
        method = partial(getattr(f, name), **kwargs)
        terms = {
            'vanilla': method(),
            'masked': method(mask=m),
            'grouped': method(groupby=c),
            'grouped_with_nulls': method(groupby=c_with_nulls),
            'both': method(mask=m, groupby=c),
            'both_with_nulls': method(mask=m, groupby=c_with_nulls),
        }

        expected = {
            'vanilla': apply_along_axis(func, 1, factor_data,),
            'masked': where(
                eyemask,
                grouped_apply(factor_data, eyemask, func),
                nan,
            ),
            'grouped': grouped_apply(
                factor_data,
                classifier_data,
                func,
            ),
            # If the classifier has nulls, we should get NaNs in the
            # corresponding locations in the output.
            'grouped_with_nulls': where(
                eyemask90,
                grouped_apply(factor_data, classifier_data_eyenulls90, func),
                nan,
            ),
            # Passing a mask with a classifier should behave as though the
            # classifier had nulls where the mask was False.
            'both': where(
                eyemask,
                grouped_apply(
                    factor_data,
                    classifier_data_eyenulls,
                    func,
                ),
                nan,
            ),
            'both_with_nulls': where(
                xmask,
                grouped_apply(
                    factor_data,
                    classifier_data_xnulls,
                    func,
                ),
                nan,
            )
        }

        self.check_terms(
            terms=terms,
            expected=expected,
            initial_workspace={
                f: factor_data,
                c: classifier_data,
                c_with_nulls: classifier_data_eyenulls90,
                Mask(): eyemask,
            },
            mask=self.build_mask(nomask),
        )

    @parameter_space(method_name=['demean', 'zscore'])
    def test_cant_normalize_non_float(self, method_name):
        class DateFactor(Factor):
            dtype = datetime64ns_dtype
            inputs = ()
            window_length = 0

        d = DateFactor()
        with self.assertRaises(TypeError) as e:
            getattr(d, method_name)()

        errmsg = str(e.exception)
        expected = (
            "{normalizer}() is only defined on Factors of dtype float64,"
            " but it was called on a Factor of dtype datetime64[ns]."
        ).format(normalizer=method_name)

        self.assertEqual(errmsg, expected)

    @parameter_space(seed=[1, 2, 3])
    def test_quantiles_unmasked(self, seed):
        permute = partial(permute_rows, seed)

        shape = (6, 6)

        # Shuffle the input rows to verify that we don't depend on the order.
        # Take the log to ensure that we don't depend on linear scaling or
        # integrality of inputs
        factor_data = permute(log1p(arange(36, dtype=float).reshape(shape)))

        f = self.f

        # Apply the same shuffle we applied to the input rows to our
        # expectations. Doing it this way makes it obvious that our
        # expectation corresponds to our input, while still testing against
        # a range of input orderings.
        permuted_array = compose(permute, partial(array, dtype=int64_dtype))
        self.check_terms(
            terms={
                '2': f.quantiles(bins=2),
                '3': f.quantiles(bins=3),
                '6': f.quantiles(bins=6),
            },
            initial_workspace={
                f: factor_data,
            },
            expected={
                # The values in the input are all increasing, so the first half
                # of each row should be in the bottom bucket, and the second
                # half should be in the top bucket.
                '2': permuted_array([[0, 0, 0, 1, 1, 1],
                                     [0, 0, 0, 1, 1, 1],
                                     [0, 0, 0, 1, 1, 1],
                                     [0, 0, 0, 1, 1, 1],
                                     [0, 0, 0, 1, 1, 1],
                                     [0, 0, 0, 1, 1, 1]]),
                # Similar for three buckets.
                '3': permuted_array([[0, 0, 1, 1, 2, 2],
                                     [0, 0, 1, 1, 2, 2],
                                     [0, 0, 1, 1, 2, 2],
                                     [0, 0, 1, 1, 2, 2],
                                     [0, 0, 1, 1, 2, 2],
                                     [0, 0, 1, 1, 2, 2]]),
                # In the limiting case, we just have every column different.
                '6': permuted_array([[0, 1, 2, 3, 4, 5],
                                     [0, 1, 2, 3, 4, 5],
                                     [0, 1, 2, 3, 4, 5],
                                     [0, 1, 2, 3, 4, 5],
                                     [0, 1, 2, 3, 4, 5],
                                     [0, 1, 2, 3, 4, 5]]),
            },
            mask=self.build_mask(self.ones_mask(shape=shape)),
        )

    @parameter_space(seed=[1, 2, 3])
    def test_quantiles_masked(self, seed):
        permute = partial(permute_rows, seed)

        # 7 x 7 so that we divide evenly into 2/3/6-tiles after including the
        # nan value in each row.
        shape = (7, 7)

        # Shuffle the input rows to verify that we don't depend on the order.
        # Take the log to ensure that we don't depend on linear scaling or
        # integrality of inputs
        factor_data = permute(log1p(arange(49, dtype=float).reshape(shape)))
        factor_data_w_nans = where(
            permute(rot90(self.eye_mask(shape=shape))),
            factor_data,
            nan,
        )
        mask_data = permute(self.eye_mask(shape=shape))

        f = F()
        f_nans = OtherF()
        m = Mask()

        # Apply the same shuffle we applied to the input rows to our
        # expectations. Doing it this way makes it obvious that our
        # expectation corresponds to our input, while still testing against
        # a range of input orderings.
        permuted_array = compose(permute, partial(array, dtype=int64_dtype))

        self.check_terms(
            terms={
                '2_masked': f.quantiles(bins=2, mask=m),
                '3_masked': f.quantiles(bins=3, mask=m),
                '6_masked': f.quantiles(bins=6, mask=m),
                '2_nans': f_nans.quantiles(bins=2),
                '3_nans': f_nans.quantiles(bins=3),
                '6_nans': f_nans.quantiles(bins=6),
            },
            initial_workspace={
                f: factor_data,
                f_nans: factor_data_w_nans,
                m: mask_data,
            },
            expected={
                # Expected results here are the same as in
                # test_quantiles_unmasked, except with diagonals of -1s
                # interpolated to match the effects of masking and/or input
                # nans.
                '2_masked': permuted_array([[-1, 0,  0,  0,  1,  1,  1],
                                            [0, -1,  0,  0,  1,  1,  1],
                                            [0,  0, -1,  0,  1,  1,  1],
                                            [0,  0,  0, -1,  1,  1,  1],
                                            [0,  0,  0,  1, -1,  1,  1],
                                            [0,  0,  0,  1,  1, -1,  1],
                                            [0,  0,  0,  1,  1,  1, -1]]),
                '3_masked': permuted_array([[-1, 0,  0,  1,  1,  2,  2],
                                            [0, -1,  0,  1,  1,  2,  2],
                                            [0,  0, -1,  1,  1,  2,  2],
                                            [0,  0,  1, -1,  1,  2,  2],
                                            [0,  0,  1,  1, -1,  2,  2],
                                            [0,  0,  1,  1,  2, -1,  2],
                                            [0,  0,  1,  1,  2,  2, -1]]),
                '6_masked': permuted_array([[-1, 0,  1,  2,  3,  4,  5],
                                            [0, -1,  1,  2,  3,  4,  5],
                                            [0,  1, -1,  2,  3,  4,  5],
                                            [0,  1,  2, -1,  3,  4,  5],
                                            [0,  1,  2,  3, -1,  4,  5],
                                            [0,  1,  2,  3,  4, -1,  5],
                                            [0,  1,  2,  3,  4,  5, -1]]),
                '2_nans': permuted_array([[0,  0,  0,  1,  1,  1, -1],
                                          [0,  0,  0,  1,  1, -1,  1],
                                          [0,  0,  0,  1, -1,  1,  1],
                                          [0,  0,  0, -1,  1,  1,  1],
                                          [0,  0, -1,  0,  1,  1,  1],
                                          [0, -1,  0,  0,  1,  1,  1],
                                          [-1, 0,  0,  0,  1,  1,  1]]),
                '3_nans': permuted_array([[0,  0,  1,  1,  2,  2, -1],
                                          [0,  0,  1,  1,  2, -1,  2],
                                          [0,  0,  1,  1, -1,  2,  2],
                                          [0,  0,  1, -1,  1,  2,  2],
                                          [0,  0, -1,  1,  1,  2,  2],
                                          [0, -1,  0,  1,  1,  2,  2],
                                          [-1, 0,  0,  1,  1,  2,  2]]),
                '6_nans': permuted_array([[0,  1,  2,  3,  4,  5, -1],
                                          [0,  1,  2,  3,  4, -1,  5],
                                          [0,  1,  2,  3, -1,  4,  5],
                                          [0,  1,  2, -1,  3,  4,  5],
                                          [0,  1, -1,  2,  3,  4,  5],
                                          [0, -1,  1,  2,  3,  4,  5],
                                          [-1, 0,  1,  2,  3,  4,  5]]),
            },
            mask=self.build_mask(self.ones_mask(shape=shape)),
        )

    def test_quantiles_uneven_buckets(self):
        permute = partial(permute_rows, 5)
        shape = (5, 5)

        factor_data = permute(log1p(arange(25, dtype=float).reshape(shape)))
        mask_data = permute(self.eye_mask(shape=shape))

        f = F()
        m = Mask()

        permuted_array = compose(permute, partial(array, dtype=int64_dtype))
        self.check_terms(
            terms={
                '3_masked': f.quantiles(bins=3, mask=m),
                '7_masked': f.quantiles(bins=7, mask=m),
            },
            initial_workspace={
                f: factor_data,
                m: mask_data,
            },
            expected={
                '3_masked': permuted_array([[-1, 0,  0,  1,  2],
                                            [0, -1,  0,  1,  2],
                                            [0,  0, -1,  1,  2],
                                            [0,  0,  1, -1,  2],
                                            [0,  0,  1,  2, -1]]),
                '7_masked': permuted_array([[-1, 0,  2,  4,  6],
                                            [0, -1,  2,  4,  6],
                                            [0,  2, -1,  4,  6],
                                            [0,  2,  4, -1,  6],
                                            [0,  2,  4,  6, -1]]),
            },
            mask=self.build_mask(self.ones_mask(shape=shape)),
        )

    def test_quantile_helpers(self):
        f = self.f
        m = Mask()

        self.assertIs(f.quartiles(), f.quantiles(bins=4))
        self.assertIs(f.quartiles(mask=m), f.quantiles(bins=4, mask=m))
        self.assertIsNot(f.quartiles(), f.quartiles(mask=m))

        self.assertIs(f.quintiles(), f.quantiles(bins=5))
        self.assertIs(f.quintiles(mask=m), f.quantiles(bins=5, mask=m))
        self.assertIsNot(f.quintiles(), f.quintiles(mask=m))

        self.assertIs(f.deciles(), f.quantiles(bins=10))
        self.assertIs(f.deciles(mask=m), f.quantiles(bins=10, mask=m))
        self.assertIsNot(f.deciles(), f.deciles(mask=m))

    @parameter_space(seed=[1, 2, 3])
    def test_clip(self, seed):
        rand = np.random.RandomState(seed)
        shape = (5, 5)
        original_min = -10
        original_max = +10
        input_array = rand.uniform(
            original_min,
            original_max,
            size=shape,
        )
        min_, max_ = np.percentile(input_array, [25, 75])
        self.assertGreater(min_, original_min)
        self.assertLess(max_, original_max)

        f = F()

        self.check_terms(
            terms={
                'clip': f.clip(min_, max_)
            },
            initial_workspace={
                f: input_array,
            },
            expected={
                'clip': np.clip(input_array, min_, max_),
            },
            mask=self.build_mask(self.ones_mask(shape=shape)),
        )


class ReprTestCase(TestCase):
    """
    Tests for term reprs.
    """

    def test_demean(self):
        r = F().demean().graph_repr()
        self.assertEqual(r, "GroupedRowTransform('demean')")

    def test_zscore(self):
        r = F().zscore().graph_repr()
        self.assertEqual(r, "GroupedRowTransform('zscore')")

    def test_winsorize(self):
        r = F().winsorize(min_percentile=.05, max_percentile=.95).graph_repr()
        self.assertEqual(r, "GroupedRowTransform('winsorize')")

    def test_recarray_field_repr(self):
        class MultipleOutputs(CustomFactor):
            outputs = ['a', 'b']
            inputs = ()
            window_length = 5

            def recursive_repr(self):
                return "CustomRepr()"

        a = MultipleOutputs().a
        b = MultipleOutputs().b

        self.assertEqual(a.graph_repr(), "CustomRepr().a")
        self.assertEqual(b.graph_repr(), "CustomRepr().b")

    def test_latest_repr(self):

        class SomeDataSet(DataSet):
            a = Column(dtype=float64_dtype)
            b = Column(dtype=float64_dtype)

        self.assertEqual(
            SomeDataSet.a.latest.graph_repr(),
            "Latest"
        )
        self.assertEqual(
            SomeDataSet.b.latest.graph_repr(),
            "Latest"
        )

    def test_recursive_repr(self):

        class DS(DataSet):
            a = Column(dtype=float64_dtype)
            b = Column(dtype=float64_dtype)

        class Input(CustomFactor):
            inputs = ()
            window_safe = True

        class HasInputs(CustomFactor):
            inputs = [Input(window_length=3), DS.a, DS.b]
            window_length = 3

        result = repr(HasInputs())
        expected = "HasInputs([Input(...), DS.a, DS.b], 3)"
        self.assertEqual(result, expected)

    def test_rank_repr(self):
        rank = DailyReturns().rank()
        result = repr(rank)
        expected = "Rank(DailyReturns(...), method='ordinal')"
        self.assertEqual(result, expected)

        recursive_repr = rank.recursive_repr()
        self.assertEqual(recursive_repr, "Rank(...)")

    def test_rank_repr_with_mask(self):
        rank = DailyReturns().rank(mask=Mask())
        result = repr(rank)
        expected = "Rank(DailyReturns(...), method='ordinal', mask=Mask(...))"
        self.assertEqual(result, expected)

        recursive_repr = rank.recursive_repr()
        self.assertEqual(recursive_repr, "Rank(...)")


class TestWindowSafety(TestCase):

    def test_zscore_is_window_safe(self):
        self.assertTrue(F().zscore().window_safe)

    @parameter_space(__fail_fast=True, is_window_safe=[True, False])
    def test_window_safety_propagates_to_recarray_fields(self, is_window_safe):

        class MultipleOutputs(CustomFactor):
            outputs = ['a', 'b']
            inputs = ()
            window_length = 5
            window_safe = is_window_safe

        mo = MultipleOutputs()

        for attr in mo.a, mo.b:
            self.assertEqual(attr.window_safe, mo.window_safe)

    def test_demean_is_window_safe_if_input_is_window_safe(self):
        self.assertFalse(F().demean().window_safe)
        self.assertFalse(F(window_safe=False).demean().window_safe)
        self.assertTrue(F(window_safe=True).demean().window_safe)

    def test_winsorize_is_window_safe_if_input_is_window_safe(self):
        self.assertFalse(
            F().winsorize(min_percentile=.05, max_percentile=.95).window_safe
        )
        self.assertFalse(
            F(window_safe=False).winsorize(
                min_percentile=.05,
                max_percentile=.95
            ).window_safe
        )
        self.assertTrue(
            F(window_safe=True).winsorize(
                min_percentile=.05,
                max_percentile=.95
            ).window_safe
        )


class TestPostProcessAndToWorkSpaceValue(ZiplineTestCase):
    @parameter_space(dtype_=(float64_dtype, datetime64ns_dtype))
    def test_reversability(self, dtype_):
        class F(Factor):
            inputs = ()
            dtype = dtype_
            window_length = 0

        f = F()
        column_data = array(
            [[0, f.missing_value],
             [1, f.missing_value],
             [2, 3]],
            dtype=dtype_,
        )

        assert_equal(f.postprocess(column_data.ravel()), column_data.ravel())

        # only include the non-missing data
        pipeline_output = pd.Series(
            data=array([0, 1, 2, 3], dtype=dtype_),
            index=pd.MultiIndex.from_arrays([
                [pd.Timestamp('2014-01-01'),
                 pd.Timestamp('2014-01-02'),
                 pd.Timestamp('2014-01-03'),
                 pd.Timestamp('2014-01-03')],
                [0, 0, 0, 1],
            ]),
        )

        assert_equal(
            f.to_workspace_value(pipeline_output, pd.Index([0, 1])),
            column_data,
        )


class TestSpecialCases(WithUSEquityPricingPipelineEngine,
                       ZiplineTestCase):
    ASSET_FINDER_COUNTRY_CODE = 'US'

    def check_equivalent_terms(self, terms):
        self.assertTrue(len(terms) > 1, "Need at least two terms to compare")
        pipe = Pipeline(terms)

        start, end = self.trading_days[[-10, -1]]
        results = self.pipeline_engine.run_pipeline(pipe, start, end)
        first_column = results.iloc[:, 0]
        for name in terms:
            assert_equal(results.loc[:, name], first_column, check_names=False)

    def test_daily_returns_is_special_case_of_returns(self):
        self.check_equivalent_terms({
            'daily': DailyReturns(),
            'manual_daily': Returns(window_length=2),
        })


class SummaryTestCase(BaseUSEquityPipelineTestCase, ZiplineTestCase):

    @parameter_space(
        seed=[1, 2, 3],
        mask=[
            np.zeros((10, 5), dtype=bool),
            ones((10, 5), dtype=bool),
            eye(10, 5, dtype=bool),
            ~eye(10, 5, dtype=bool),
        ]
    )
    def test_summary_methods(self, seed, mask):
        """Test that summary funcs work the same as numpy NaN-aware funcs.
        """
        rand = np.random.RandomState(seed)
        shape = (10, 5)
        data = rand.randn(*shape)
        data[~mask] = np.nan

        workspace = {F(): data}
        terms = {
            'mean': F().mean(),
            'sum': F().sum(),
            'median': F().median(),
            'min': F().min(),
            'max': F().max(),
            'stddev': F().stddev(),
            'notnull_count': F().notnull_count(),
        }

        with ignore_nanwarnings():
            expected = {
                'mean': as_column(np.nanmean(data, axis=1)),
                'sum': as_column(np.nansum(data, axis=1)),
                'median': as_column(np.nanmedian(data, axis=1)),
                'min': as_column(np.nanmin(data, axis=1)),
                'max': as_column(np.nanmax(data, axis=1)),
                'stddev': as_column(np.nanstd(data, axis=1)),
                'notnull_count': as_column((~np.isnan(data)).sum(axis=1)),
            }

        # Make sure we have test coverage for all summary funcs.
        self.assertEqual(set(expected), summary_funcs.names)

        self.check_terms(
            terms=terms,
            expected=expected,
            initial_workspace=workspace,
            mask=self.build_mask(ones(shape)),
            check=check_allclose
        )

    @parameter_space(
        seed=[4, 5, 6],
        mask=[
            np.zeros((10, 5), dtype=bool),
            ones((10, 5), dtype=bool),
            eye(10, 5, dtype=bool),
            ~eye(10, 5, dtype=bool),
        ]
    )
    def test_built_in_vs_summary(self, seed, mask):
        """Test that summary funcs match normalization functions.
        """
        rand = np.random.RandomState(seed)
        shape = (10, 5)
        data = rand.randn(*shape)
        data[~mask] = np.nan

        workspace = {F(): data}
        terms = {
            'demean': F().demean(),
            'alt_demean': F() - F().mean(),

            'zscore': F().zscore(),
            'alt_zscore': (F() - F().mean()) / F().stddev(),

            'mean': F().mean(),
            'alt_mean': F().sum() / F().notnull_count(),
        }

        result = self.run_terms(
            terms,
            initial_workspace=workspace,
            mask=self.build_mask(ones(shape)),
        )

        assert_equal(result['demean'], result['alt_demean'])
        assert_equal(result['zscore'], result['alt_zscore'])

    @parameter_space(
        seed=[100, 200, 300],
        mask=[
            np.zeros((10, 5), dtype=bool),
            ones((10, 5), dtype=bool),
            eye(10, 5, dtype=bool),
            ~eye(10, 5, dtype=bool),
        ]
    )
    def test_complex_expression(self, seed, mask):
        rand = np.random.RandomState(seed)
        shape = (10, 5)
        data = rand.randn(*shape)
        data[~mask] = np.nan

        workspace = {F(): data}
        terms = {
            'rescaled': (F() - F().min()) / (F().max() - F().min()),
        }

        with ignore_nanwarnings():
            mins = as_column(np.nanmin(data, axis=1))
            maxes = as_column(np.nanmax(data, axis=1))

        expected = {
            'rescaled': (data - mins) / (maxes - mins),
        }

        self.check_terms(
            terms,
            expected,
            initial_workspace=workspace,
            mask=self.build_mask(ones(shape)),
        )

    @parameter_space(
        seed=[40, 41, 42],
        mask=[
            np.zeros((10, 5), dtype=bool),
            ones((10, 5), dtype=bool),
            eye(10, 5, dtype=bool),
            ~eye(10, 5, dtype=bool),
        ],
        # Three ways to mask:
        # 1. Don't mask.
        # 2. Mask by passing mask parameter to summary methods.
        # 3. Mask by having non-True values in the root mask.
        mask_mode=('none', 'param', 'root'),
    )
    def test_summaries_after_fillna(self, seed, mask, mask_mode):
        rand = np.random.RandomState(seed)
        shape = (10, 5)

        # Create data with a mix of NaN and non-NaN values.
        with_nans = np.where(mask, rand.randn(*shape), np.nan)

        # Create a version with NaNs filled with -1s.
        with_minus_1s = np.where(mask, with_nans, -1)

        kwargs = {}
        workspace = {F(): with_nans}

        # Call each summary method with mask=Mask().
        if mask_mode == 'param':
            kwargs['mask'] = Mask()
            workspace[Mask()] = mask

        # Take the mean after applying a fillna of -1 to ensure that we ignore
        # masked locations properly.
        terms = {
            'mean': F().fillna(-1).mean(**kwargs),
            'sum': F().fillna(-1).sum(**kwargs),
            'median': F().fillna(-1).median(**kwargs),
            'min': F().fillna(-1).min(**kwargs),
            'max': F().fillna(-1).max(**kwargs),
            'stddev': F().fillna(-1).stddev(**kwargs),
            'notnull_count': F().fillna(-1).notnull_count(**kwargs),
        }

        with ignore_nanwarnings():
            if mask_mode == 'none':
                # If we aren't masking, we should expect the results to see the
                # -1s.
                expected_input = with_minus_1s
            else:
                # If we are masking, we should expect the results to see NaNs.
                expected_input = with_nans

            expected = {
                'mean': as_column(np.nanmean(expected_input, axis=1)),
                'sum': as_column(np.nansum(expected_input, axis=1)),
                'median': as_column(np.nanmedian(expected_input, axis=1)),
                'min': as_column(np.nanmin(expected_input, axis=1)),
                'max': as_column(np.nanmax(expected_input, axis=1)),
                'stddev': as_column(np.nanstd(expected_input, axis=1)),
                'notnull_count': as_column(
                    (~np.isnan(expected_input)).sum(axis=1),
                ),
            }

        # Make sure we have test coverage for all summary funcs.
        self.assertEqual(set(expected), summary_funcs.names)

        if mask_mode == 'root':
            root_mask = self.build_mask(mask)
        else:
            root_mask = self.build_mask(ones_like(mask))

        self.check_terms(
            terms=terms,
            expected=expected,
            initial_workspace=workspace,
            mask=root_mask,
            check=check_allclose
        )

    def test_repr(self):

        class MyFactor(CustomFactor):
            window_length = 1
            inputs = ()

            def recursive_repr(self):
                return "MyFactor()"

        f = MyFactor()

        for method in summary_funcs.names:
            summarized = getattr(f, method)()
            self.assertEqual(
                repr(summarized),
                "MyFactor().{}()".format(method),
            )
            self.assertEqual(
                summarized.recursive_repr(),
                "MyFactor().{}()".format(method),
            )
