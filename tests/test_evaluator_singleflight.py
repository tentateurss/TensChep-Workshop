from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

from poecraft.data import trade
from poecraft.engine import evaluator as E
import test_trade_metrics as metrics_fixture


class SingleFlight(unittest.TestCase):
    def setUp(self):
        self.fixture = metrics_fixture.TradeMetrics()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        trade.leagues()

    def call(self, text='same', league='Covens', **kwargs):
        return E._evaluate_with_context(text, league, data=object(), **kwargs)

    def outcome(self):
        return {'kind': 'unique', 'listings': [], 'warnings': []}

    def paired_failure(self, error):
        entered, joined, release = threading.Event(), threading.Event(), threading.Event()
        count = trade.count_request

        def record(event):
            if event == 'evaluation.shared_wait':
                joined.set()
            count(event)

        def fail(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Owner was not released')
            raise error

        with patch.object(E, '_evaluate', side_effect=fail) as compute, \
                patch.object(trade, 'count_request', side_effect=record), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.call)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.call)
                self.assertTrue(joined.wait(5))
            finally:
                release.set()
            self.assertIs(first.exception(timeout=5), error)
            self.assertIs(second.exception(timeout=5), error)
            self.assertEqual(compute.call_count, 1)
        self.assertFalse(E._INFLIGHT)
        self.assertFalse(E._CACHE)
        with patch.object(E, '_evaluate', return_value=self.outcome()) as retry:
            self.call()
            retry.assert_called_once()

    def test_owner_exception_releases_waiters_and_allows_retry(self):
        self.paired_failure(ValueError('controlled failure'))

    def test_owner_cancellation_releases_waiters_and_allows_retry(self):
        self.paired_failure(CancelledError())

    def test_owner_interrupt_releases_waiters_and_allows_retry(self):
        self.paired_failure(KeyboardInterrupt())

    def test_waiter_timeout_or_cancellation_does_not_cancel_owner(self):
        for failure in (TimeoutError, CancelledError):
            with self.subTest(failure=failure.__name__):
                E._CACHE.clear()
                entered, release = threading.Event(), threading.Event()

                class InterruptedWait(Future):
                    def result(self, timeout=None):
                        self_timeout.assertEqual(timeout, E.config.EVAL_WAIT_BUDGET + trade.TIMEOUT)
                        if failure is TimeoutError:
                            return super().result(timeout=0.01)
                        raise failure()

                self_timeout = self

                def compute(*args):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('Owner was not released')
                    return self.outcome()

                with patch.object(E, 'Future', InterruptedWait), \
                        patch.object(E, '_evaluate', side_effect=compute) as evaluate, \
                        ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(self.call)
                    try:
                        self.assertTrue(entered.wait(5))
                        second = pool.submit(self.call)
                        self.assertIsInstance(second.exception(timeout=2), failure)
                        self.assertEqual(len(E._INFLIGHT), 1)
                    finally:
                        release.set()
                    self.assertEqual(first.result(timeout=5)['kind'], 'unique')
                    self.call()
                    self.assertEqual(evaluate.call_count, 1)
                self.assertFalse(E._INFLIGHT)

    def test_different_items_leagues_and_modifier_values_run_independently(self):
        for args in [('other', 'Covens'), ('same', 'Standard'), ('same\n+20 to Life', 'Covens')]:
            with self.subTest(args=args):
                E._CACHE.clear()
                barrier = threading.Barrier(2)

                def compute(*unused):
                    barrier.wait(timeout=5)
                    return self.outcome()

                with patch.object(E, '_evaluate', side_effect=compute) as evaluate, \
                        ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(self.call)
                    second = pool.submit(self.call, *args)
                    first.result(timeout=10)
                    second.result(timeout=10)
                    self.assertEqual(evaluate.call_count, 2)
                self.assertFalse(E._INFLIGHT)

    def test_timeless_options_remain_per_caller(self):
        def extras(out, socket, equivalents, league):
            return dict(out, socket=socket, equivalents=equivalents)

        with patch.object(E, '_evaluate', return_value=self.outcome()) as compute, \
                patch.object(E, '_timeless_extras', side_effect=extras):
            first = self.call(socket='a', equivalents=['one'])
            second = self.call(socket='b', equivalents=['two'])
            self.assertEqual((first['socket'], second['socket']), ('a', 'b'))
            self.assertEqual((first['equivalents'], second['equivalents']), (['one'], ['two']))
            self.assertEqual(compute.call_count, 1)
