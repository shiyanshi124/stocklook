"""
MIT License

Copyright (c) 2017 Zeke Barge

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import logging
from functools import partial
from stocklook.config import config
from stocklook.crypto.gdax.order import GdaxOrder, GdaxOrderCancellationError
logger = logging.getLogger(__name__)
logger.setLevel(config.get('LOG_LEVEL', logging.DEBUG))


class OrderLockError(Exception):
    pass


class GdaxMMOrder(GdaxOrder):
    """
    A market maker order with price management and analysis helpers.
    """
    def __init__(self, market_maker, *args, op_order=None, **kwargs):
        """
        :param market_maker: stocklook.crypto.gdax.market_maker.GdaxMarketMaker
        :param args: stocklook.crypto.gdax.order.GdaxOrder(*args)
        :param op_order: (GdaxMMOrder, default None)
        :param kwargs: stocklook.crypto.gdax.order.GdaxOrder(**kwargs)

        Example:
            o = GdaxMMOrder(market_maker,
                            Gdax(),
                            'ETH-USD',
                            side='buy',
                            order_type='limit',
                            price=300.05,
                            size=0.05
            )
        """
        self.market_maker = market_maker
        self._op_order = op_order
        self._fill_chain = list()
        self._locked = False
        self._prices = list()
        self._unlock_method = None
        self._cycle_number = 0
        GdaxOrder.__init__(self, *args, **kwargs)

    @property
    def m(self):
        """
        Short-hand accessor to GdaxMMOrder.market_maker
        :return:
        """
        return self.market_maker

    @property
    def op_order(self):
        """
        Applies only to sell orders. Returns the opposite side
        of the trade. Used to calculate stop-outs and profit/loss.
        :return:
        """
        return self._op_order

    @property
    def cycle_number(self):
        """
        The number of Market Maker order cycles the order has been through.
        A high number of cycles means the order is stale.
        :return:
        """
        return self._cycle_number

    @property
    def stop_amount(self):
        """
        The price at which the sell order is stopped out of the trade.
        based on the GdaxMarketMaker.stop_pct property.
        :return:
        """
        stop_pct = self.m.stop_pct
        op_order = self.op_order
        if not stop_pct:
            return None
        if op_order is None:
            return None

        if self.side == 'buy':
            return None

        op_price = op_order.price
        return op_price - (op_price * stop_pct)

    @property
    def locked(self):
        """
        Returns True if the order has been locked, False otherwise.
        A locked order shouldnt get adjusted price/size-wise.
        :return:
        """
        return self.locked

    def lock(self, unlock_method=None):
        """
        Locks the order with an optional unlock method.

        :param unlock_method: (callable, default None)
            This method should be able to evaluate the GdaxMMOrder
            and return false or raise an exception when an unlock SHOULD NOT occur.
        :return:
        """
        if self._locked:
            raise OrderLockError("Already locked: {}".format(self))
        self._locked = True
        self._unlock_method = unlock_method

    def unlock(self):
        """
        Unlocks the order, passing self to the
        unlock method if one was provided.
        :return:
        """
        if self._unlock_method is not None:
            try:
                if not self._unlock_method(self):
                    raise OrderLockError("Failed to unlock order")
            except Exception as e:
                raise OrderLockError("Unlock method failure: {}".format(e))

        self._locked = False

    def get_volume_until_fill(self):
        """
        Returns the volume required to fill
        the order based on the given price.
        :return:
        """
        if self.side == 'buy':
            return self.m.book_feed.get_bid_depth(self.price)
        return self.m.book_feed.get_ask_depth(self.price)

    def get_amount_above_spread(self, spread=None):
        """
        Returns the difference between the order price and the current bid/ask based
        on a given spread target.

        :param spread: (int, float, default GdaxMMOrder.market_maker.max_spread)

        :param bid: (float, default stocklook.crypto.gdax.feeds.book_feed.BookSnapshot.lowest_ask)
        :param ask: (float, default stocklook.crypto.gdax.feeds.book_feed.BookSnapshot.highest_bid)
        :return:
        """
        if spread is None:
            spread = self.m.max_spread
        spread_bit = spread / 3
        snap = self.m.get_book_snapshot()

        if self.side == 'sell':
            bid = snap.bids[0][0]
            max_price = bid + spread

            for _ in range(5):
                depth_check = snap.calculate_ask_depth(max_price)
                if depth_check >= 30 or depth_check == 0:
                    break
                max_price += spread_bit
                logger.info("ask depth {} @ ${}".format(depth_check, max_price))

            return round(self.price - max_price, 2)

        elif self.side == 'buy':
            ask = snap.asks[0][0]
            min_price = ask - spread

            for _ in range(5):
                depth_check = snap.calculate_bid_depth(min_price)
                if depth_check >= 30 or depth_check == 0:
                    break
                min_price -= spread_bit
                depth_check = snap.calculate_bid_depth(min_price)
                logger.info("bid depth {} @ ${}".format(depth_check, min_price))
            return round(self.price - min_price, 2)

    def get_pnl(self, price=None):
        """
        Sell orders will return the amount of profit that is/will be generated
        when the position is closed based on the current or provided price.
        :param price: (float, default None)
        :return: (float)
        """
        op = self._op_order
        if op is not None:
            if price is None:
                price = self.price
            if self.side == 'buy':
                buy_spend = self.size * price
                sell_spend = op.size * op.price
            else:
                buy_spend = op.size * op.price
                sell_spend = self.size * price
            return round(sell_spend - buy_spend, 2)
        return None

    def register_op_order(self, order):
        """
        Registers the opposide side of the trade to the order.
        For example:
            GdaxMMOrder1 BUY $300
            GdaxMMOrder2 SELL $300.40
            GdaxMMOrder1.register_op_order(GdaxMMOrder2)

        :param order:
        :return:
        """
        o_side = order.side
        my_side = self.side
        o_op_order = getattr(order, '_op_order', None)

        if o_side == my_side and o_op_order is not None:
            # This is an order replacement
            # of the same type.
            self._op_order = o_op_order
        elif o_side != my_side:
            # The order is a different side
            # and therefore an opposite order.
            self._op_order = order
        else:
            raise AttributeError("Cannot register op_order "
                                 "of same type '{}'".format(o_side))

    def register_order_cycle(self):
        """
        Increments the GdaxMMOrder.cycle_number - called by the
        MarketMaker on each order evaluation cycle.
        :return:
        """
        self._cycle_number += 1

    def get_price_adjusted_to_spread(self,
                                     spread=None,
                                     aggressive=True,
                                     amount_above=None,
                                     factor=0.8,
                                     min_profit=0.01):
        """

        :param spread:
        :param aggressive:
        :param amount_above:
        :param factor:
        :param min_profit:
        :return:
        """

        if not amount_above:
            if not spread:
                if aggressive:
                    # aggressive orders use tight spreads
                    spread = self.m.min_spread
                else:
                    spread = self.m.max_spread
            amount_above = self.get_amount_above_spread(spread=spread)

        if amount_above:
            price = round(self.price - (amount_above * factor), 2)
        else:
            price = round(self.price, 2)

        op_order = self.op_order
        if op_order is not None and min_profit is not None:
            min_price = op_order.price + min_profit
            if price < min_price:
                price = min_price

        return price

    def get_price_incremented(self,
                              p,
                              other_prices,
                              cap_out=None,
                              increment=True,
                              step=0.03,
                              min_profit=0.01,
                              _force=False):
        """
        Increments or decrements a price ensuring no other prices are within 1 step.

        :param p: (float)
            The price to manipulate.
        :param other_prices: (list)
            The other prices to evaluate. The return price should not be
        :param cap_out:
        :param increment:
        :param step:
        :param min_profit:
        :param _force:
        :return:
        """
        try:
            other_prices.remove(p)
        except ValueError:
            pass

        # make a range around the current price by 1 step
        max_p = p + step
        min_p = p - step
        check_p = [x for x in other_prices
                   if x > min_p and x < max_p]
        p = round(p, 2)

        while check_p:
            if increment:
                p += step
            else:
                p -= step
            # refresh the range based on the new
            p = round(p, 2)
            max_p = p + step
            min_p = p - step
            check_p = [x for x in other_prices
                       if x >= min_p
                       and x <= max_p]

        if cap_out is not None and _force is False:
            # Check to see if price has exceeded the cap
            if not increment and p < cap_out:
                # means we were decreasing price
                # and the price ended up underneath the cap
                # We'll increment the price back around the other prices.
                return self.get_price_incremented(p,
                                                  other_prices,
                                                  cap_out=cap_out,
                                                  increment=True,
                                                  step=step,
                                                  _force=True)
            elif increment and p > cap_out:
                # increasing price exceeded cap out means
                # we'll decrement the priec back around the othe prices.
                return self.get_price_incremented(p,
                                                  other_prices,
                                                  cap_out=cap_out,
                                                  increment=False,
                                                  step=step,
                                                  _force=True)

        if self.side == 'sell' and min_profit is not None:
            # We dont want to sell below minimum spread vs our
            # buy order unless we're stopped out.
            p2 = self.get_price_target_via_op(min_profit)
            if p > self.stop_amount:
                # not stopped out
                # so we'll check profit
                if p2 > p:
                    # the price would have been below target.
                    # We'll just increment it again with force
                    return self.get_price_incremented(p2,
                                                      other_prices,
                                                      cap_out=cap_out,
                                                      increment=True,
                                                      step=step,
                                                      _force=True)
        return p

    def get_price_adjusted_to_other_prices(self, other_prices=None, aggressive=True, step=0.03, min_profit=0.01):
        """
        Looks at other prices in the book and returns a unique price that is higher or lower than the others
        based on order side and if the caller is looking to be aggressive.
        :param other_prices: (list, default None)
            An optional list of prices to compare against.
            buy orders default to prices found in current GdaxMarketMaker.buy_orders
            sell orders default to prices found in current GdaxMarketMaker.sell_orders

        :param aggressive: (bool, default True)
            True attempts to make buy prices higher than others
        :param step:
        :param min_profit:
        :return:
        """
        if other_prices is None:
            if self.side == 'buy':
                other_prices = list(self.m.buy_orders.values())
            else:
                other_prices = list(self.m.sell_orders.values())

        my_min = self.get_price_adjusted_to_spread(spread=None,
                                                   aggressive=aggressive,
                                                   min_profit=min_profit)
        if not other_prices:
            return my_min

        # cap out either 5 steps higher or lower
        # cap may be ignored on sell orders
        f = len(other_prices)
        cap_out = (my_min+(step*f) if self.side == 'sell'
                   else my_min-(step*f))

        _adj_price = partial(self.get_price_incremented,
                             cap_out=cap_out,
                             step=step)

        def adj_price(p, increment=True):
            return _adj_price(p, other_prices, increment=increment)

        min_price = min(other_prices)
        max_price = max(other_prices)
        max_and_step = max_price + step
        min_and_step = min_price - step

        if self.side == 'buy':

            if aggressive:
                # Aggressive buys need to be near top
                if my_min >= max_and_step:
                    return adj_price(my_min, increment=True)
                else:
                    return adj_price(my_min, increment=False)

            else:
                # Non aggressive buys - position somewhere healthy
                return adj_price(my_min, increment=False)

        elif self.side == 'sell':
            if aggressive:
                # Aggressive sells should be near bottom
                if my_min >= min_and_step:
                    return adj_price(my_min, increment=False)
                else:
                    return adj_price(my_min, increment=True)
            else:
                # Non aggressive sells - position somewhere healthy
                return adj_price(my_min, increment=True)

    def get_price_adjusted_to_profit_target(self, min_profit=0.01):
        """
        Returns a price that a sell order needs to be sold at
        in order to reach a given profit dollar amount.
        :param min_profit: (float, int, default 0.01)
            The minimum $ of profit that the sale must bring.
        :return: (None, float)

        """
        price = self.price
        pnl = self.get_pnl(price)
        if pnl is None:
            return self.price
        while pnl < min_profit:
            price += 0.01
        return price

    def get_price_target_via_op(self, min_diff=0.01):
        try:
            return self.op_order.price + min_diff
        except AttributeError:
            return self.price

    def get_other_order_prices(self, side='buy'):
        """
        Returns a list of order prices for a given side (buy or sell).
        :param side:
        :return:
        """
        return [round(o.price, 2) for o in self.m._orders.values()
                if o.side == side]

    def get_price_adjusted_to_ticker(self, price=None, ticker=None, aggressive=True, adjust_vs_open=True):
        """
        Returns a price adjusted against the current ticker.
            - buy price greater than ticker gets decreased by the spread
            - sell price lower than ticker gets increased by the spread

        :param price: (float, default GdaxMMOrder.price)
            The price to evaluate against the ticker.

        :param ticker: (dict, default GdaxMMOrder.market_maker.book_feed.get_current_ticker())
            A dictionary containing ticker details.

        :param aggressive: (bool, default True)
            aggressive orders use GdaxMMOrder.market_maker.min_spread
            non-aggressive prices use GdaxMMOrder.market_market.max_spread
            This is used to increment or decrement the price

        :param adjust_vs_open (bool, default True)
             True adjusts price in half-spread increments (more profitably) to make unique
                  from other buy or sell orders.
            False just returns the ticker-adjusted price.
        :return:
        """
        if ticker is None:
            ticker = self.m.book_feed.get_current_ticker()
        if price is None:
            price = self.price

        if aggressive:
            spread = self.m.min_spread
        else:
            spread = self.m.max_spread

        if ticker:
            ticker_price = float(ticker['price'])
            if self.side == 'buy':
                if price >= ticker_price - spread:
                    price = ticker_price - spread

            elif price <= ticker_price + spread:
                price = ticker_price + spread

        o_prices = self.get_other_order_prices(side=self.side)
        spread_add = round(spread / 2, 2)

        while price in o_prices:
            if self.side == 'buy':
                # decrease buy price
                price -= spread_add
            else:
                # increase sell price
                price += spread_add

        return price

    def get_price_adjusted_to_wall(self, min_idx=2, wall_size=50, bump_value=0.01):
        """
        Returns the price nearest the wall, sell order placed just below the wall
        and buy orders placed just above the wall.
        :param min_idx:
        :param wall_size:
        :param bump_value:
        :return:
        """
        snap = self.m.get_book_snapshot()
        data = (snap.bids if self.side == 'buy' else snap.asks)

        for idx, contents in enumerate(data):
            # minimum size and index position
            if contents[1] >= wall_size and idx >= min_idx:
                if self.side == 'buy':
                    # price above the wall
                    return contents[0] + bump_value
                else:
                    # price below the wall
                    return contents[0] - bump_value