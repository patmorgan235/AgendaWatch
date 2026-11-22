from datetime import date

from agendawatch.http import FetchError
from agendawatch.sources import CivicClerkSource, CivicWebSource, DocsPublishedSource, GranicusSource, DocTableSource, LegistarSource, LinkPageSource, NovusSource, build_sources

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


COR_TABLE = """
<table class="govAccess-reTable-4"><thead><tr>
 <th>&nbsp;DATE</th><th>AGENDA</th><th>PACKET</th><th>PRESENTATIONS / VIDEO</th><th colspan="2">ACTIONS / MINUTES</th>
</tr></thead><tbody>
<tr><td>November 23, 2026</td>
 <td><a href="https://www.cor.net/home/showpublisheddocument/47501/639304000000000000">City Council Meeting</a></td>
 <td>&nbsp;</td><td>Presentations<br>Meeting Video</td><td colspan="2">Approved Minutes</td></tr>
<tr><td>November 16, 2026</td>
 <td><a href="/home/showpublisheddocument/47473/639299221076900000">Special Called Meeting: Board &amp; Commission Interviews</a></td>
 <td>&nbsp;</td><td>Presentations</td><td>Approved Minutes</td></tr>
<tr><td>November 9, 2026</td>
 <td><a href="https://www.cor.net/home/showpublisheddocument/47415/639293225086370000">City Council Meeting</a></td>
 <td><a href="https://www.cor.net/home/showpublisheddocument/47419/639293918044870000">Agenda Packet</a></td>
 <td><a href="https://www.cor.net/home/showpublisheddocument/47469/639298398353970000">Presentations</a><br>
     <a href="https://richardsontx.new.swagit.com/videos/408119">Meeting Video</a></td>
 <td colspan="2">Approved Minutes</td></tr>
<tr><td>November 30, 2026</td><td>City Council - No Meeting</td><td></td><td></td><td></td></tr>
<tr><td>September 8, 2025</td>
 <td><a href="https://www.cor.net/home/showpublisheddocument/40000/1">City Council Meeting</a></td><td></td><td></td><td></td></tr>
</tbody></table>"""


def test_doc_table_reads_agenda_and_packet_columns():
    url = "https://www.cor.net/government/city-council/council-meeting-documents"
    src = DocTableSource(name="Richardson", city="Richardson", http=FakeHttp({url: COR_TABLE}), urls=[url])
    docs = src.list_documents(START, END)
    got = [(d.meeting_date, d.kind, d.body, d.url.rsplit("/", 2)[1]) for d in docs]
    assert got == [
        (date(2026, 11, 23), "agenda", "City Council Meeting", "47501"),
        (date(2026, 11, 16), "agenda", "Special Called Meeting: Board & Commission Interviews", "47473"),
        (date(2026, 11, 9), "agenda", "City Council Meeting", "47415"),
        (date(2026, 11, 9), "packet", "City Council Meeting", "47419"),
    ]  # presentations/video ignored, the 2025 row is outside the window, "No Meeting" has no links
    assert docs[1].url.startswith("https://www.cor.net/home/")  # relative link resolved
    assert docs[3].title == "City Council Meeting packet"
    assert all(d.is_upcoming is (d.meeting_date >= date.today()) for d in docs)


def test_doc_table_without_a_table_is_an_error():
    url = "https://example.gov/docs"
    src = DocTableSource(name="X", city="X", http=FakeHttp({url: "<p>Access Denied</p>"}), urls=[url])
    try:
        src.list_documents(START, END)
    except Exception as exc:  # noqa: BLE001
        assert "No meeting table" in str(exc)
    else:
        raise AssertionError("expected an error")


def test_user_agent_browser_applies_only_to_that_sites_host():
    http = FakeHttp({})
    build_sources([
        {"name": "Richardson", "type": "doc_table", "user_agent": "browser",
         "urls": ["https://www.cor.net/government/city-council/council-meeting-documents"]},
        {"name": "Addison", "type": "link_page", "urls": ["https://agendas.addisontx.gov/x"]},
    ], http)
    assert list(http.host_headers) == ["www.cor.net"]
    assert http.host_headers["www.cor.net"]["_http2"] == "yes"


def test_browser_host_routing_without_network():
    from agendawatch.http import BROWSER_HEADERS, Http

    http = Http(delay=0)
    http.set_browser_host("www.cor.net")
    assert http._is_browser_host("https://www.cor.net/home/showpublisheddocument/1/2")
    assert not http._is_browser_host("https://richardsontx.new.swagit.com/views/614")
    assert http._host_headers["www.cor.net"] == BROWSER_HEADERS


def test_civicclerk_events_and_paging():
    api = "https://rowletttx.api.civicclerk.com/v1"
    page1 = {"value": [
        {"id": 109, "eventName": "City Council Meeting", "categoryName": "City Council",
         "startDateTime": "2026-12-01T19:00:00Z", "publishedFiles": [
             {"fileId": 7504, "type": "Agenda", "name": "12.01.26 City Council Meeting Agenda"},
             {"fileId": 7505, "type": "Agenda Packet", "name": "12.01.26 City Council Meeting Agenda Packet"},
             {"fileId": 7580, "type": "Minutes", "name": "Minutes"}]},
        {"id": 110, "eventName": "City Council Meeting", "categoryName": "City Council",
         "startDateTime": "2026-12-15T19:00:00Z", "publishedFiles": []},  # not posted yet
    ], "@odata.nextLink": f"{api}/Events?$skip=2"}
    page2 = {"value": [
        {"id": 111, "eventName": "Parks Board", "categoryName": "Boards",
         "startDateTime": "2026-12-03T18:00:00Z", "publishedFiles": [{"fileId": 9, "type": "Agenda"}]},
        {"id": 112, "eventName": "Cancelled No Quorum_Zoning Board of Adjustment", "categoryName": "Boards",
         "startDateTime": "2026-12-04T18:00:00Z", "publishedFiles": [{"fileId": 10, "type": "Agenda"}]},
    ]}
    http = FakeHttp({f"{api}/Events": page1, f"{api}/Events?$skip=2": page2})
    src = CivicClerkSource(name="Rowlett", city="Rowlett", http=http, tenant="rowletttx")
    docs = src.list_documents(START, END)
    assert [(d.meeting_date, d.kind, d.url.rsplit("=", 2)[1].split(",")[0]) for d in docs] == [
        (date(2026, 12, 1), "agenda", "7504"),
        (date(2026, 12, 1), "packet", "7505"),
        (date(2026, 12, 3), "agenda", "9"),
    ]
    assert docs[0].url == f"{api}/Meetings/GetMeetingFileStream(fileId=7504,plainText=false)"
    assert docs[1].title == "City Council Meeting agenda packet"
    assert docs[0].detail_url == "https://rowletttx.portal.civicclerk.com/event/109/files"

    only_council = CivicClerkSource(name="R", city="R", http=http, tenant="rowletttx", bodies=["City Council"])
    assert {d.body for d in only_council.list_documents(START, END)} == {"City Council Meeting"}


RTC_PAGE = """
<div class="table-row"><div class="table-cell"><span class="month">August 18</span></div>
 <div class="table-cell"><a class="link" href="/getContentAsset/aaaa/dfc3/agendapacketaugust22.pdf">Agenda (including all meeting materials)</a></div>
</div>
<div class="table-row"><div class="table-cell"><span class="month">November 5</span></div>
 <div class="table-cell">
  <a class="link" href="/getContentAsset/4fa4/dfc3/agendartc11-05-2026.pdf?language=en-US">Agenda</a>
  <a class="link" href="/getContentAsset/59a4/dfc3/agendapacketrtc11052026.pdf?language=en-US">Agenda (including all meeting materials)</a>
  <a class="link" href="/getContentAsset/13f4/dfc3/presentationsrtc11052026.pdf?language=en-US">Presentations </a>
 </div>
 <div class="table-cell"><a class="link" href="/getContentAsset/beb9/dfc3/minutesrtc11052026.pdf?language=en-US">Minutes</a></div>
</div>"""


def test_rtc_link_page_dates_from_file_names():
    url = "https://www.nctcog.org/trans/about/committees/regional-transportation-council"
    src = LinkPageSource(name="RTC", city="RTC", http=FakeHttp({url: RTC_PAGE}), urls=[url],
                         link_pattern=r"/getContentAsset/.+\.pdf", link_text_pattern="^Agenda",
                         meeting_name="Regional Transportation Council", skip_undated=True)
    docs = src.list_documents(START, END)
    assert [(d.meeting_date, d.body, d.url.split("/")[-1].split("?")[0]) for d in docs] == [
        (date(2026, 11, 5), "Regional Transportation Council", "agendartc11-05-2026.pdf"),
        (date(2026, 11, 5), "Regional Transportation Council", "agendapacketrtc11052026.pdf"),
    ]


def test_civicweb_document_center():
    base = "https://dallascounty.civicweb.net"
    a = lambda href, text: f'<a href="{href}">{text}</a>'
    http = FakeHttp({
        f"{base}/filepro/documents": a("/filepro/documents/1009", "Agendas") + a("/filepro/documents/9754", "Minutes"),
        f"{base}/filepro/documents/1009": a("/filepro/documents/1021", "Commissioners Court")
            + a("/filepro/documents/15878", "Executive Session"),
        f"{base}/filepro/documents/1021": a("/filepro/documents/1040992", "2026") + a("/filepro/documents/1011482", "2025"),
        f"{base}/filepro/documents/1040992":
            a("/document/1066462", "Commissioners Court - Nov 17 2026 - Agenda - Html")
            + a("/document/1066461", "Commissioners Court - Nov 17 2026 - Agenda - Pdf")
            + a("/document/1064083", "Commissioners Court - Sep 01 2026 - Agenda - Html")
            + a("/document/1066470", "Commissioners Court - Special Called Session - 24 Nov 2026 - Agenda - Html"),
    })
    src = CivicWebSource(name="Dallas County", city="Dallas County", http=http, base_url=base)
    docs = src.list_documents(START, END)
    assert [(d.meeting_date, d.body, d.url) for d in docs] == [
        (date(2026, 11, 17), "Commissioners Court", f"{base}/document/1066462"),
        (date(2026, 11, 24), "Commissioners Court - Special Called Session", f"{base}/document/1066470"),
    ]  # Pdf skipped by default, Sep 1 outside the window, 2025 folder not opened
    assert f"{base}/filepro/documents/1011482" not in http.calls
    both = CivicWebSource(name="DC", city="DC", http=http, base_url=base, format="both")
    assert len(both.list_documents(START, END)) == 3


def test_link_page_caps_after_date_filter():
    old = "".join(f'<li><a href="/a?ADID={i}">January {i % 28 + 1}, 2019</a></li>' for i in range(50))
    url1, url2 = "https://hv.example/Archive?AMID=50", "https://hv.example/Archive?AMID=57"
    http = FakeHttp({url1: old, url2: '<li><a href="/a?ADID=999">November 3, 2026 - Special Work Session</a></li>'})
    src = LinkPageSource(name="HV", city="HV", http=http, urls=[url1, url2],
                         link_pattern=r"ADID=\d+", meeting_name="City Council", max_links=10)
    assert [d.url for d in src.list_documents(START, END)] == ["https://hv.example/a?ADID=999"]


def test_docs_published_outline_and_utc_dates():
    api = "https://api.docassembler.io/api"
    org_id = "da44"
    agenda_type = {"Name": "Agenda"}
    listing = [
        {"MeetingType": "City Council Meeting", "MeetingDate": "2026-11-11T00:00:00",  # Nov 10, 6 pm Central
         "AgendaDocumentId": "a1", "WP_DocumentType": agenda_type},
        {"MeetingType": "City Council Meeting", "MeetingDate": "2026-11-11T00:00:00",
         "AgendaDocumentId": "m1", "WP_DocumentType": {"Name": "Minutes"}},
        {"MeetingType": "Old Meeting", "MeetingDate": "2026-06-23T14:00:00",
         "AgendaDocumentId": "o1", "WP_DocumentType": agenda_type},
    ]
    doc = {"Title": "City Council Meeting - November 10 2026", "MeetingType": "City Council Meeting",
           "MeetingVenue": "Fort Worth City Hall", "WP_Sections": [
               {"Number": "9.0", "Title": "Non-Consent Agenda", "WP_Items": [
                   {"Number": "9.1", "Title": "Interlocal agreement with Trinity Metro for TEXRail",
                    "WP_Attachments": [{"Title": "Trinity Metro ILA exhibit"}]}]}]}
    http = FakeHttp({
        f"{api}/organisation/fortworthtexas": {"Id": org_id},
        f"{api}/documents/{org_id}": listing,
        f"{api}/document/a1": doc,
    })
    src = DocsPublishedSource(name="Fort Worth", city="Fort Worth", http=http, org="fortworthtexas")
    docs = src.list_documents(START, END)
    assert len(docs) == 1
    d = docs[0]
    assert d.meeting_date == date(2026, 11, 10)
    assert d.url == "https://docspublished.io/fortworthtexas/document/a1" and d.kind == "items"
    assert "9.1 Interlocal agreement with Trinity Metro for TEXRail" in d.inline_text
    assert "Attachment: Trinity Metro ILA exhibit" in d.inline_text


def test_link_page_exclude_pattern_checks_the_row():
    url = "https://ro.example/AgendaCenter"
    page = """<table>
    <tr><td><strong>City Council Meeting</strong></td><td><a href="/AgendaCenter/ViewFile/Agenda/_11172026-421">Nov 17</a></td></tr>
    <tr><td><strong>Municipal Court Docket 11-12-2026</strong></td><td><a href="/AgendaCenter/ViewFile/Agenda/_11122026-420">Nov 12</a></td></tr>
    </table>"""
    src = LinkPageSource(name="RO", city="RO", http=FakeHttp({url: page}), urls=[url],
                         link_pattern=r"/AgendaCenter/ViewFile/Agenda/_\d{8}-\d+", exclude_pattern="municipal court")
    docs = src.list_documents(START, END)
    assert [(d.meeting_date, d.body) for d in docs] == [(date(2026, 11, 17), "City Council Meeting Nov 17")]


GRANICUS = """<div id="upcoming"><table class="listingTable">
<tr><th>Name</th><th>Date</th><th></th></tr>
<tr><td class="listItem">Commissioners Court</td><td class="listItem">Dec 1, 2026</td>
 <td class="listItem"><a href="//dc.granicus.com/AgendaViewer.php?view_id=26&event_id=812">Agenda</a></td></tr>
</table></div>
<table class="listingTable">
<tr><td class="listItem">Commissioners Court</td><td class="listItem"><span style="display:none;">1794902400</span>Nov 17, 2026</td>
 <td class="listItem"><select name="multiDocSelect"><option selected disabled>Documents...</option>
 <option value="//dc.granicus.com/AgendaViewer.php?view_id=26&clip_id=2102">Agenda</option>
 <option value="//dc.granicus.com/MinutesViewer.php?view_id=26&clip_id=2102&doc_id=x">11-17-26</option></select></td></tr>
<tr><td class="listItem">Commissioners Court</td><td class="listItem">Mar 4, 2025</td>
 <td class="listItem"><select><option value="//dc.granicus.com/AgendaViewer.php?view_id=26&clip_id=1500">Agenda</option></select></td></tr>
</table>"""


def test_granicus_upcoming_and_archived():
    url = "https://dc.granicus.com/ViewPublisher.php?view_id=26"
    src = GranicusSource(name="Denton County", city="Denton County", http=FakeHttp({url: GRANICUS}), url=url)
    docs = src.list_documents(START, END)
    assert [(d.meeting_date, d.body, d.url) for d in docs] == [
        (date(2026, 12, 1), "Commissioners Court", "https://dc.granicus.com/AgendaViewer.php?view_id=26&event_id=812"),
        (date(2026, 11, 17), "Commissioners Court", "https://dc.granicus.com/AgendaViewer.php?view_id=26&clip_id=2102"),
    ]  # minutes ignored, 2025 meeting outside the window


def test_link_page_name_from_table_cell():
    url = "https://agenda.example/OnBaseAgendaOnline/Meetings/Search?dropid=4&mtids=0"
    page = """<table>
    <tr><td>City Council Regular Meeting</td><td>City Council</td><td>12/1/2026 5:00:00 PM</td>
        <td><a href="/OnBaseAgendaOnline/Documents/DownloadFile/City_Council_Regular_Meeting_5760_Agenda_12_1_2026_5_00_00_PM.pdf?documentType=1&amp;meetingId=5760">Agenda</a></td></tr>
    <tr><td>Planning &amp; Zoning Commission Meeting</td><td>Planning and Zoning Commission</td><td>11/23/2026 6:30:00 PM</td>
        <td><a href="/OnBaseAgendaOnline/Documents/DownloadFile/PZ_5751_Minutes_11_23_2026.pdf?documentType=3&amp;meetingId=5751">Minutes</a></td></tr>
    </table>"""
    src = LinkPageSource(name="Frisco", city="Frisco", http=FakeHttp({url: page}), urls=[url],
                         link_pattern=r"/DownloadFile/[^?]+\.pdf\?documentType=1&", name_cell=0, skip_undated=True)
    docs = src.list_documents(START, END)
    assert [(d.meeting_date, d.body) for d in docs] == [(date(2026, 12, 1), "City Council Regular Meeting")]
