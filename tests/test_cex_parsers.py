"""CEX adapter parsing and quote history (offline, using real message shapes)."""

from collector.cex import BinanceFeed, BybitFeed, OkxFeed, Quote


def test_binance_book_ticker():
    q = BinanceFeed("ETHUSDC").parse(
        '{"u":1,"s":"ETHUSDC","b":"2242.40","B":"13.1","a":"2242.41","A":"7.69"}')
    assert (q.bid, q.ask, q.bid_qty, q.ask_qty) == (2242.40, 2242.41, 13.1, 7.69)


def test_bybit_orderbook1_and_ack_ignored():
    f = BybitFeed("ETHUSDC")
    assert f.parse('{"success":true,"op":"subscribe"}') is None
    q = f.parse('{"topic":"orderbook.1.ETHUSDC","type":"snapshot","data":{"s":"ETHUSDC",'
                '"b":[["2242.78","0.161"]],"a":[["2242.79","1.8"]],"u":5}}')
    assert (q.bid, q.ask, q.ask_qty) == (2242.78, 2242.79, 1.8)


def test_okx_bbo():
    f = OkxFeed("ETH-USDC")
    assert f.parse("pong") is None
    q = f.parse('{"arg":{"channel":"bbo-tbt"},"data":[{"asks":[["2242.6","0.09","0","1"]],'
                '"bids":[["2242.59","1.1","0","1"]],"ts":"1"}]}')
    assert (q.bid, q.ask) == (2242.59, 2242.6)


def test_quote_at_returns_last_quote_before_ts_and_drops_crossed_books():
    f = BinanceFeed("ETHUSDC")
    for ts, bid in [(1.0, 100.0), (2.0, 101.0), (3.0, 102.0)]:
        f._push(Quote("binance", bid, bid + 0.01, 1, 1, ts))
    f._push(Quote("binance", 105.0, 104.0, 1, 1, 3.5))  # crossed: ignored
    assert f.quote_at(2.5).bid == 101.0
    assert f.quote_at(0.5) is None
    assert f.latest.bid == 102.0
