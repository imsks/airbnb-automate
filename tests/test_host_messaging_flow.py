"""Exercise the real Playwright messaging controls without contacting Airbnb."""

import asyncio
from contextlib import asynccontextmanager
from html import escape

import pytest
from playwright.async_api import async_playwright

from app.agent.chat_reader import SessionExpired
from app.messaging_errors import DeliveryUnconfirmed, MessageRejected
from app.models import Listing
from app.outreach import (
    _click_send_message,
    choose_inbox_row,
    choose_thread_link,
    _is_logged_in,
    _open_contact_or_message_cta,
    _send_message_to_host,
    _wait_for_delivery,
    _wait_for_visible_composer,
    parse_rejection_terms,
)

#: Verbatim from the banner Airbnb showed when it refused a real draft,
#: curly apostrophes and all.
REJECTION_BANNER = (
    "Sorry, we can\u2019t send your message yet. Links and contact info can\u2019t be "
    "shared until after a reservation is confirmed. Please remove the info below "
    "before sending: \u201cInstagram\u201d"
)

#: Airbnb's second refusal, shown when the draft named social accounts. It is
#: worded as advice but the message is still withheld.
OFF_PLATFORM_BANNER = (
    "Why risk it? Stay on Airbnb. It looks like this message refers to "
    "communicating with a Host outside of Airbnb. We can\u2019t help with refunds or "
    "rebooking if you book off Airbnb\u2019s platform."
)


@asynccontextmanager
async def local_page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.route("**/*", lambda route: route.abort())
        page = await context.new_page()
        try:
            yield page
        finally:
            await browser.close()


def test_contact_waits_for_host_link_not_booking_controls():
    async def scenario():
        async with local_page() as page:
            await page.set_content('''
                <button onclick="document.body.dataset.clicked='booking'">
                    Check availability
                </button>
                <a id="contact" hidden href="/contact_host/42/send_message"
                   onclick="event.preventDefault(); document.body.dataset.clicked='contact'">
                    Message host
                </a>
                <script>setTimeout(() => document.querySelector('#contact').hidden = false, 1200)</script>
            ''')
            assert await _open_contact_or_message_cta(page)
            assert await page.locator("body").get_attribute("data-clicked") == "contact"

    asyncio.run(scenario())


@pytest.fixture
def immediate_waits(monkeypatch):
    async def yield_loop(_milliseconds):
        await asyncio.sleep(0)

    monkeypatch.setattr("app.outreach._async_sleep_ms", yield_loop)


def test_composer_supports_contenteditable(immediate_waits):
    async def scenario():
        async with local_page() as page:
            await page.set_content(
                '<div role="textbox" contenteditable="true" aria-label="Write a message"></div>'
            )
            composer = await _wait_for_visible_composer(page)
            await composer.fill("Hello Peggy")
            assert await composer.inner_text() == "Hello Peggy"

    asyncio.run(scenario())


def test_airbnb_contact_host_message_field_from_live_dom(immediate_waits):
    async def scenario():
        async with local_page() as page:
            await page.set_content('''
                <h1>Contact Peggy</h1>
                <textarea id="contactHostMessage" name="contactHostMessage" aria-label="Message the host"></textarea>
                <button data-testid="send-message-button">Send message</button>
                <button data-testid="homes-pdp-cta-btn">Check availability</button>
                <textarea hidden></textarea>
            ''')
            composer = await _wait_for_visible_composer(page)
            assert await composer.get_attribute("id") == "contactHostMessage"

    asyncio.run(scenario())


def test_login_dialog_is_not_reported_as_missing_composer(immediate_waits):
    async def scenario():
        async with local_page() as page:
            await page.set_content('''
                <div role="dialog">
                    <h1>Welcome back, Sachin</h1>
                    <button>Log in</button>
                </div>
            ''')
            with pytest.raises(SessionExpired):
                await _wait_for_visible_composer(page)

    asyncio.run(scenario())


def test_google_login_dialog_is_classified_as_authentication(immediate_waits):
    async def scenario():
        async with local_page() as page:
            await page.set_content('<h1>Log in with Google</h1><button>Continue with Google</button>')
            with pytest.raises(SessionExpired):
                await _wait_for_visible_composer(page)

    asyncio.run(scenario())


def test_oauth_popup_is_never_treated_as_airbnb_session():
    async def scenario():
        async with local_page() as page:
            await page.route("https://accounts.google.com/**", lambda route: route.fulfill(
                body='<h1>Sign in</h1><button aria-label="Profile">Choose account</button>'
            ))
            await page.goto("https://accounts.google.com/login")
            assert not await _is_logged_in(page)

    asyncio.run(scenario())


def test_newest_inbox_row_for_that_host_and_city_is_the_thread():
    rows = [
        ("1", "https://www.airbnb.co.in/guest/messages/1", "Enquiry sent · Udaipur. Adil. You: Enquiry sent."),
        ("2", "https://www.airbnb.co.in/guest/messages/2", "Enquiry sent · Jaisalmer. Akshay. You: Enquiry sent."),
    ]
    assert choose_inbox_row(rows, host_name="Akshay", location="Jaisalmer, Rajasthan, India")[0] == "2"


def test_another_hosts_row_in_the_same_city_is_not_this_send():
    rows = [
        ("111", "https://www.airbnb.co.in/guest/messages/111", "Enquiry sent · Alleppey. Binoy. You: Enquiry sent."),
    ]
    assert choose_inbox_row(
        rows, host_name="Pearl", location="Alleppey (Alappuzha), Kerala, India"
    ) == (None, "")


def test_inbox_row_id_is_used_when_the_link_is_only_a_hash(immediate_waits):
    """Airbnb's inbox row address is '#'. The thread id is on the row itself."""

    async def scenario():
        async with local_page() as page:
            async def route(request):
                url = request.request.url
                if "/guest/messages/777" in url:
                    html = '<div data-testid="message-list">Hello</div>'
                elif "/guest/inbox" in url or "/hosting/inbox" in url:
                    html = (
                        '<a data-testid="inbox_list_777" href="#">'
                        "Enquiry sent · Udaipur. Adil. You: Enquiry sent."
                        "</a>"
                    )
                else:
                    html = '<textarea name="message"></textarea>'
                await request.fulfill(status=200, content_type="text/html", body=html)

            await page.route("https://www.airbnb.co.in/**", route)
            await page.goto("https://www.airbnb.co.in/rooms/42")
            result = await _wait_for_delivery(
                page,
                "Hello Adil, the lake looks lovely.",
                timeout_ms=200,
                clicked=True,
                host_name="Adil",
                location="Udaipur, Rajasthan, India",
            )
            assert result[0] == "777"

    asyncio.run(scenario())


def test_message_preview_picks_the_thread_it_was_sent_to():
    links = [
        ("1", "https://www.airbnb.co.in/guest/messages/1", "Asha — see you in May"),
        ("2", "https://www.airbnb.co.in/guest/messages/2", "Hello Peggy, the garden looks lovely."),
    ]
    assert choose_thread_link(links, "Hello Peggy, the garden looks lovely.", "Asha")[0] == "2"


def test_one_host_row_identifies_the_thread_when_the_preview_is_cut_off():
    links = [
        ("1", "https://www.airbnb.co.in/guest/messages/1", "Conversation with Asha"),
        ("9", "https://www.airbnb.co.in/guest/messages/9", "Conversation with Peggy"),
    ]
    assert choose_thread_link(links, "totally different wording", "Peggy")[0] == "9"


def test_two_unnamed_threads_are_not_guessed():
    links = [
        ("1", "https://www.airbnb.co.in/guest/messages/1", "Inbox"),
        ("2", "https://www.airbnb.co.in/guest/messages/2", "Inbox"),
    ]
    assert choose_thread_link(links, "Hello Peggy", "") == (None, "")


def test_a_listing_send_is_confirmed_by_opening_the_inbox(immediate_waits):
    """The listing stays put after Send. The new thread is in the inbox."""

    body = "Hello Peggy, the garden looks lovely."

    async def scenario():
        async with local_page() as page:
            async def route(request):
                url = request.request.url
                if "/guest/messages/555" in url:
                    html = '<div data-testid="message-list">' + body + "</div>"
                elif any(part in url for part in ("/messaging", "/guest/inbox", "/hosting/inbox")):
                    html = (
                        '<a href="/guest/messages/111">Enquiry sent · Kochi. Binoy.</a>'
                        '<a data-testid="inbox_list_555" href="#">'
                        "Enquiry sent · Alleppey. Peggy. You: Enquiry sent."
                        "</a>"
                    )
                else:
                    html = '<textarea name="message"></textarea>'
                await request.fulfill(status=200, content_type="text/html", body=html)

            await page.route("https://www.airbnb.co.in/**", route)
            await page.goto("https://www.airbnb.co.in/rooms/42")
            result = await _wait_for_delivery(
                page, body, timeout_ms=200, clicked=True, host_name="Peggy"
            )
            assert result[0] == "555"
            assert result[1].endswith("/guest/messages/555")

    asyncio.run(scenario())


def test_the_thread_already_open_in_the_inbox_is_not_this_send(immediate_waits, monkeypatch):
    """Opening the inbox lands on the previous conversation. That is not the send."""
    monkeypatch.setattr("app.outreach._INBOX_LOOKUP_SECONDS", 0)

    async def scenario():
        async with local_page() as page:
            async def route(request):
                url = request.request.url
                if "/guest/inbox" in url or "/hosting/inbox" in url:
                    html = (
                        '<a href="/guest/messages/111">Enquiry sent · Kochi. Binoy.</a>'
                        '<a data-testid="inbox_list_111" href="#">'
                        "Enquiry sent · Kochi. Binoy. You: Enquiry sent."
                        "</a>"
                    )
                else:
                    html = '<textarea name="message"></textarea>'
                await request.fulfill(status=200, content_type="text/html", body=html)

            await page.route("https://www.airbnb.co.in/**", route)
            await page.goto("https://www.airbnb.co.in/rooms/42")
            with pytest.raises(DeliveryUnconfirmed, match="no conversation was open"):
                await _wait_for_delivery(
                    page,
                    "Hello Pearl, the cottage looks lovely.",
                    timeout_ms=200,
                    clicked=True,
                    host_name="Pearl",
                    location="Alleppey (Alappuzha), Kerala, India",
                )

    asyncio.run(scenario())


def test_filling_a_composer_is_not_delivery_confirmation():
    async def scenario():
        async with local_page() as page:
            await page.set_content('<textarea name="message">Hello Peggy</textarea>')
            with pytest.raises(DeliveryUnconfirmed):
                await _wait_for_delivery(page, "Hello Peggy", timeout_ms=200)

    asyncio.run(scenario())


def test_missing_receipt_does_not_click_send_again():
    async def scenario():
        async with local_page() as page:
            await page.set_content('''
                <button onclick="this.dataset.clicks = Number(this.dataset.clicks || 0) + 1">Send</button>
            ''')
            assert await _click_send_message(page)
            with pytest.raises(DeliveryUnconfirmed):
                await _wait_for_delivery(page, "Hello Peggy", timeout_ms=200)
            assert await page.get_by_role("button", name="Send", exact=True).get_attribute("data-clicks") == "1"

    asyncio.run(scenario())


def test_airbnb_refusal_names_the_term_it_objected_to():
    assert parse_rejection_terms(REJECTION_BANNER) == ["Instagram"]


def test_ordinary_page_text_is_not_mistaken_for_a_refusal():
    assert parse_rejection_terms('Peggy says: "Instagram is fine by me!"') == []


def test_a_refused_message_is_reported_as_not_sent(immediate_waits):
    """Airbnb says it did not send, so this must never look like a maybe-send."""

    async def scenario():
        async with local_page() as page:
            await page.set_content("<div>" + escape(REJECTION_BANNER) + "</div>")
            with pytest.raises(MessageRejected) as caught:
                await _wait_for_delivery(page, "Hello Peggy", timeout_ms=2000)
            assert caught.value.terms == ["Instagram"]

    asyncio.run(scenario())


def test_off_platform_warning_is_treated_as_a_refusal(immediate_waits):
    """Worded as advice, but Airbnb withholds the message all the same."""

    async def scenario():
        async with local_page() as page:
            await page.set_content("<div>" + escape(OFF_PLATFORM_BANNER) + "</div>")
            with pytest.raises(MessageRejected, match="off-platform"):
                await _wait_for_delivery(page, "Hello Peggy", timeout_ms=2000)

    asyncio.run(scenario())


def test_a_refusal_is_not_downgraded_to_an_uncertain_delivery(immediate_waits):
    async def scenario():
        async with local_page() as page:
            async def route(request):
                if "/rooms/" in request.request.url:
                    html = '<h1>Room</h1><a href="/contact_host/42/send_message">Message host</a>'
                else:
                    html = ('<h1>Message Peggy</h1><textarea name="message"></textarea>'
                            '<button onclick="document.querySelector(\'#banner\').hidden=false">Send message</button>'
                            '<div id="banner" hidden>' + escape(REJECTION_BANNER) + '</div>')
                await request.fulfill(status=200, content_type="text/html", body=html)

            await page.route("https://www.airbnb.co.in/**", route)
            with pytest.raises(MessageRejected):
                await _send_message_to_host(
                    page, Listing(id="42", title="Room"), "Find me on Instagram"
                )

    asyncio.run(scenario())


def test_real_browser_send_requires_persisted_conversation(immediate_waits):
    async def scenario():
        body = "Hi Peggy! Your garden looks lovely."
        checks, thread_loads = [], []
        async with local_page() as page:
            async def route(request):
                path = request.request.url
                if "/rooms/" in path:
                    html = '<h1>Garden room</h1><a href="/contact_host/42/send_message">Message host</a>'
                elif "/contact_host/" in path:
                    html = '''<h1>Message Peggy</h1><textarea name="message"></textarea>
                        <button onclick="location.href='/guest/inbox/987654'">Send message</button>'''
                else:
                    thread_loads.append(path)
                    html = '<div role="group" data-item-id="outbound-1">' + escape(body) + '</div>'
                await request.fulfill(status=200, content_type="text/html", body=html)

            await page.route("https://www.airbnb.co.in/**", route)

            async def final_check():
                checks.append("authorized")
                assert await page.locator('textarea[name="message"]').input_value() == body

            result = await _send_message_to_host(
                page, Listing(id="42", title="Garden room"), body, before_send=final_check
            )
            assert result == ("987654", "https://www.airbnb.co.in/guest/inbox/987654")
            assert checks == ["authorized"]
            assert len(thread_loads) == 2

    asyncio.run(scenario())


def test_visible_conversation_confirms_delivery_without_the_message_bubble(immediate_waits):
    """Airbnb shows the thread, but the new bubble is not in the old selectors."""

    async def scenario():
        async with local_page() as page:
            async def route(request):
                path = request.request.url
                if "/rooms/" in path:
                    html = '<h1>Garden room</h1><a href="/contact_host/42/send_message">Message host</a>'
                elif "/contact_host/" in path:
                    html = '''<h1>Message Peggy</h1><textarea name="message"></textarea>
                        <button onclick="location.href='/hosting/inbox/folder/all/thread/555'">Send message</button>'''
                else:
                    html = '<div data-testid="message-list"><h1>Conversation with Peggy</h1></div>'
                await request.fulfill(status=200, content_type="text/html", body=html)

            await page.route("https://www.airbnb.co.in/**", route)
            result = await _send_message_to_host(
                page, Listing(id="42", title="Garden room"), "Hello Peggy, the garden looks lovely."
            )
            assert result == (
                "555",
                "https://www.airbnb.co.in/hosting/inbox/folder/all/thread/555",
            )

    asyncio.run(scenario())