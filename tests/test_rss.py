import unittest

from nycti.rss.client import parse_rss_feed


class RSSClientTests(unittest.TestCase):
    def test_parse_rss_feed_extracts_items(self) -> None:
        feed = parse_rss_feed(
            """
            <rss version="2.0">
              <channel>
                <title>Example News</title>
                <item>
                  <title>First &amp; Best</title>
                  <link>https://example.com/first</link>
                  <guid>first-guid</guid>
                  <description><![CDATA[<p>Short summary</p>]]></description>
                  <pubDate>Mon, 13 Apr 2026 12:00:00 GMT</pubDate>
                </item>
              </channel>
            </rss>
            """,
            feed_url="https://example.com/rss",
        )

        self.assertEqual(feed.title, "Example News")
        self.assertEqual(len(feed.items), 1)
        self.assertEqual(feed.items[0].identity, "first-guid")
        self.assertEqual(feed.items[0].title, "First & Best")
        self.assertEqual(feed.items[0].summary, "Short summary")

    def test_parse_atom_feed_extracts_alternate_link(self) -> None:
        feed = parse_rss_feed(
            """
            <feed xmlns="http://www.w3.org/2005/Atom">
              <title>Atom News</title>
              <entry>
                <title>Atom Item</title>
                <id>tag:example.com,2026:item</id>
                <link rel="alternate" href="https://example.com/atom-item" />
                <updated>2026-04-13T12:00:00Z</updated>
              </entry>
            </feed>
            """,
            feed_url="https://example.com/atom",
        )

        self.assertEqual(feed.title, "Atom News")
        self.assertEqual(feed.items[0].identity, "tag:example.com,2026:item")
        self.assertEqual(feed.items[0].link, "https://example.com/atom-item")


if __name__ == "__main__":
    unittest.main()
