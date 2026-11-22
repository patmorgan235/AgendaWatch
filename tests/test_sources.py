from datetime import date

from agendawatch.http import FetchError
from agendawatch.sources import LegistarSource, LinkPageSource, NovusSource

from helpers import FakeHttp

START, END = date(2026, 10, 1), date(2026, 12, 31)


def test_legistar_api():
    events = [
        {"EventId": 1, "EventBodyName": "City Council", "EventDate": "2026-11-04T00:00:00",
         "EventAgendaFile": "https://legistar.example/agenda1.pdf", "EventInSiteURL": "https://x/Meeting1"},
        {"EventId": 2, "EventBodyName": "Park Board", "EventDate": "2026-11-05T00:00:00",
         "EventAgendaFile": "https://legistar.example/agenda2.pdf"},
        {"EventId": 3, "EventBodyName": "City Council", "EventDate": "2026-11-18T00:00:00",
         "EventAgendaFile": None},  # agenda not posted yet
    ]
    http = FakeHttp({"https://webapi.legistar.com/v1/cityofdallas/events": events})
    src = LegistarSource(name="Dallas", city="Dallas", http=http, client="cityofdallas", bodies=["City Council"])
    docs = src.list_documents(START, END)
    assert [d.url for d in docs] == ["https://legistar.example/agenda1.pdf"]
    assert docs[0].meeting_date == date(2026, 11, 4) and docs[0].detail_url == "https://x/Meeting1"


def test_legistar_item_titles_and_attachment_names():
    events = [{"EventId": 7, "EventBodyName": "City Council", "EventDate": "2026-11-04T00:00:00",
               "EventAgendaFile": "https://l/a.pdf", "EventInSiteURL": "https://l/m7"}]
    items = [{"EventItemAgendaNumber": "12", "EventItemTitle": "Interlocal agreement with DART",
              "EventItemMatterAttachments": [{"MatterAttachmentName": "GMP funding exhibit",
                                              "MatterAttachmentHyperlink": "https://l/att.pdf"}]}]
    http = FakeHttp({
        "https://webapi.legistar.com/v1/fb/events": events,
        "https://webapi.legistar.com/v1/fb/events/7/eventitems": items,
    })
    src = LegistarSource(name="FB", city="FB", http=http, client="fb", attachments="names")
    docs = src.list_documents(START, END)
    inline = [d for d in docs if d.kind == "items"][0]
    assert "12 Interlocal agreement with DART" in inline.inline_text
    assert "Attachment: GMP funding exhibit" in inline.inline_text


LEGISTAR_CALENDAR = """
<table><tr>
 <td><a href="DepartmentDetail.aspx?ID=1">City Council</a></td><td>10/27/2026</td><td>5:45 PM</td>
 <td><a href="MeetingDetail.aspx?ID=1457108&GUID=CB01&Options=info|&Search=">Meeting details</a></td>
 <td><a href="View.ashx?M=A&ID=1457108&GUID=CB01">Agenda</a></td></tr>
<tr><td>City Council</td><td>8/11/2025</td>
 <td><a href="View.ashx?M=A&ID=99&GUID=OLD">Agenda Packet</a></td></tr>
</table>"""


def test_legistar_falls_back_to_calendar_page():
    http = FakeHttp({
        "https://webapi.legistar.com/v1/carrolltontx/events": FetchError("HTTP 403"),
        "https://carrolltontx.legistar.com/Calendar.aspx": LEGISTAR_CALENDAR,
    })
    src = LegistarSource(name="Carrollton", city="Carrollton", http=http, client="carrolltontx")
    docs = src.list_documents(START, END)
    assert len(docs) == 1  # the 2025 meeting is outside the window
    d = docs[0]
    assert d.url == "https://carrolltontx.legistar.com/View.ashx?M=A&ID=1457108&GUID=CB01"
    assert d.meeting_date == date(2026, 10, 27) and d.body == "City Council"
    assert "MeetingDetail.aspx" in d.detail_url
    assert any("website calendar" in n for n in src.notes)


NOVUS = """
<table>
<tr><td></td><td>11/05/26</td><td>City Council Meeting<br>City Council Meeting</td><td></td>
 <td><img alt="View Agenda HTML" src="images/icon_files.gif"></td>
 <td><a href="DisplayAgendaPDF.ashx?MeetingID=3701"><img alt="Agenda PDF" src="images/ico_pdf.gif"></a></td></tr>
<tr><td></td><td>10/27/26</td><td>Planning &amp; Zoning Commission</td><td>CANCELLED</td>
 <td><a href="DisplayAgendaPDF.ashx?MeetingID=3699">pdf</a></td></tr>
</table>"""


def test_novus_listing():
    base = "https://plano.novusagenda.com/agendapublic"
    http = FakeHttp({base + "/MeetingsResponsive.aspx": NOVUS})
    src = NovusSource(name="Plano", city="Plano", http=http, base_url=base)
    docs = src.list_documents(START, END)
    assert len(docs) == 1
    assert docs[0].url == base + "/DisplayAgendaPDF.ashx?MeetingID=3701"
    assert docs[0].meeting_date == date(2026, 11, 5)
    assert docs[0].body == "City Council Meeting"


SWAGIT = """
<table>
<tr><td>City Council</td><td>Oct 26, 2026</td><td>02h 42m</td>
 <td><a href="https://richardsontx.new.swagit.com/videos/405071">Video</a>
     <a href="https://richardsontx.new.swagit.com/videos/405071/agenda">Agenda</a></td></tr>
<tr><td>City Council Work Session</td><td>Oct 19, 2026</td><td>40m 01s</td>
 <td><a href="/videos/404349">Video</a> <a href="/videos/404349/agenda">Agenda</a></td></tr>
</table>"""


def test_swagit_link_page():
    url = "https://richardsontx.new.swagit.com/views/614"
    http = FakeHttp({url: SWAGIT})
    src = LinkPageSource(name="Richardson", city="Richardson", http=http, urls=[url],
                         link_pattern=r"/videos/\d+/agenda", link_text_pattern="^agenda$")
    docs = src.list_documents(START, END)
    assert [d.meeting_date for d in docs] == [date(2026, 10, 26), date(2026, 10, 19)]
    assert docs[1].url == "https://richardsontx.new.swagit.com/videos/404349/agenda"
    assert docs[0].body == "City Council"
    assert docs[1].body == "City Council Work Session"


def test_link_page_follows_meeting_pages():
    listing = '<a href="agenda_publish.cfm?id=&mt=ALL&dsp=ag&seq=7601">Council Meeting 11/10/2026</a>'
    meeting = '<p>Council Meeting</p><a href="docs/2026/CM/20261110_7601/AGENDApacket_7601.pdf">Packet</a>'
    base = "https://agendas.addisontx.gov/"
    http = FakeHttp({
        base + "agenda_publish.cfm": listing,
        base + "agenda_publish.cfm?id=&mt=ALL&dsp=ag&seq=7601": meeting,
    })
    src = LinkPageSource(name="Addison", city="Addison", http=http, urls=[base + "agenda_publish.cfm"],
                         link_pattern=r"/docs/\d{4}/[^?#]+\.pdf", follow_pattern=r"seq=\d+")
    docs = src.list_documents(START, END)
    assert len(docs) == 1
    assert docs[0].url.endswith("AGENDApacket_7601.pdf")
    assert docs[0].meeting_date == date(2026, 11, 10)
