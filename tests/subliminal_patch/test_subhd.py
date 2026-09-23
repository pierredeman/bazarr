# -*- coding: utf-8 -*-
import io
import shutil
from pathlib import Path
from zipfile import ZipFile

import pytest
from subliminal.exceptions import ProviderError
from subliminal_patch.core import Episode, Movie
from subliminal_patch.exceptions import ForbiddenError, TooManyRequests
from subliminal_patch.extensions import provider_registry
from subliminal_patch.providers.subhd import SubHDProvider, SubHDSubtitle, _file_languages
from subzero.language import Language


BASE = 'https://subhd.tv'
DATA = Path(__file__).parent / 'data' / 'subhd'
SRT = b'1\r\n00:00:01,000 --> 00:00:02,000\r\nTest subtitle\r\n'


@pytest.fixture
def provider():
    with SubHDProvider() as instance:
        yield instance


@pytest.fixture
def movie():
    return Movie('Interstellar.2014.2160p.BluRay.x265-SWTYBLZ.mkv', 'Interstellar',
                 year=2014, imdb_id='tt0816692', resolution='2160p', source='Blu-ray')


def subtitle(filename='test.srt', language=Language('zho')):
    return SubHDSubtitle(language, 'Cm0tsS', filename, 'Interstellar.2014.1080p.BluRay',
                         ['Interstellar'], 'tt0816692', 2014, 'movie')


def search_html(subtitle_id='Cm0tsS'):
    return f'''<div class="bg-white shadow-sm rounded-3 mb-4">
      <div class="view-text"><a href="/a/{subtitle_id}">Interstellar.2014</a></div>
      <span class="fw-bold">简体</span><span class="fw-bold">英语</span></div>'''


def register_download(requests_mock, body=SRT, extension='srt'):
    requests_mock.get(BASE + '/a/Cm0tsS', text=(DATA / 'movie.html').read_text())
    requests_mock.post(BASE + '/api/sub/prepare-download',
                       json={'success': True, 'url': '/down/Cm0tsS?ticket=fresh'})
    requests_mock.get(BASE + '/down/Cm0tsS?ticket=fresh', text='<button sid="Cm0tsS"></button>')
    requests_mock.post(BASE + '/api/sub/down',
                       json={'success': True, 'pass': True, 'url': f'https://dl.subhd.me/test.{extension}'})
    requests_mock.get(f'https://dl.subhd.me/test.{extension}', content=body)


def test_provider_is_registered():
    assert provider_registry['subhd'] is SubHDProvider


@pytest.mark.parametrize('filename,expected', [
    ('Movie.chs.srt', {Language('zho')}),
    ('Movie.CHT.ASS', {Language('zho', 'TW')}),
    ('Movie.zh-TW.srt', {Language('zho', 'TW')}),
    ('Movie.zh-Hans.srt', {Language('zho')}),
    ('Movie.Chs&Eng.ass', {Language('zho'), Language('eng')}),
    ('Movie.简英.ass', {Language('zho'), Language('eng')}),
    ('Movie.繁體.srt', {Language('zho', 'TW')}),
    ('Englishman.2014.srt', set()),
])
def test_file_languages(filename, expected):
    assert _file_languages(filename) == expected


def test_bilingual_subtitle_is_returned_as_chinese_only(provider, movie, requests_mock):
    requests_mock.get(BASE + '/search/Interstellar', text=(DATA / 'search.html').read_text())
    requests_mock.get(BASE + '/a/Cm0tsS', text=(DATA / 'movie.html').read_text())
    subs = provider.list_subtitles(movie, {Language('zho'), Language('eng')})
    assert len(subs) == 1
    assert subs[0].language == Language('zho')
    assert subs[0].filename == '1774886230488.ass'
    assert {'title', 'year', 'imdb_id', 'resolution'} <= subs[0].get_matches(movie)
    assert len(requests_mock.request_history) == 2


def test_simplified_chinese_country_alias(provider, movie, requests_mock):
    requests_mock.get(BASE + '/search/Interstellar', text=search_html())
    requests_mock.get(BASE + '/a/Cm0tsS', text=(DATA / 'movie.html').read_text())
    subs = provider.list_subtitles(movie, {Language('zho', 'CN')})
    assert len(subs) == 1
    assert subs[0].language == Language('zho', 'CN')


@pytest.mark.parametrize('language', [Language('fra'), Language('eng')])
def test_no_network_for_unsupported_languages(provider, movie, requests_mock, language):
    assert provider.list_subtitles(movie, {language}) == []
    assert not requests_mock.called


def test_search_only_first_page(provider, requests_mock):
    html = search_html() + '<a href="/search/Interstellar/2">Next</a>'
    requests_mock.get(BASE + '/search/Interstellar', text=html)
    assert list(provider._search('Interstellar', {Language('zho')})) == ['Cm0tsS']
    assert requests_mock.call_count == 1


@pytest.mark.parametrize('imdb_id,year', [('tt1234567', 2014), ('tt0816692', 2015)])
def test_reject_different_movie(provider, movie, requests_mock, imdb_id, year):
    movie.imdb_id, movie.year = imdb_id, year
    requests_mock.get(BASE + '/search/Interstellar', text=search_html())
    requests_mock.get(BASE + '/a/Cm0tsS', text=(DATA / 'movie.html').read_text())
    assert provider.list_subtitles(movie, {Language('zho')}) == []


def test_alternative_title_fallback(provider, movie, requests_mock):
    movie.title = '星际穿越'
    movie.alternative_titles = ['Interstellar']
    requests_mock.get(BASE + '/search/星际穿越', text='<html></html>')
    requests_mock.get(BASE + '/search/Interstellar', text=search_html())
    requests_mock.get(BASE + '/a/Cm0tsS', text=(DATA / 'movie.html').read_text())
    assert len(provider.list_subtitles(movie, {Language('zho')})) == 1


def test_pack_selects_exact_episode(provider, requests_mock):
    video = Episode('Three-Body.S01E02.mkv', 'Three-Body', 1, 2, series_imdb_id='tt20242042')
    requests_mock.get(BASE + '/search/Three-Body%20S01', text=search_html('zYrQnc'))
    requests_mock.get(BASE + '/search/Three-Body', text=search_html('zYrQnc'))
    requests_mock.get(BASE + '/a/zYrQnc', text=(DATA / 'pack.html').read_text())
    subs = provider.list_subtitles(video, {Language('zho')})
    assert len(subs) == 1
    assert 'S01E02' in subs[0].filename
    assert subs[0].is_pack
    assert {'series', 'season', 'episode', 'series_imdb_id'} <= subs[0].get_matches(video)
    video.episode = 31
    assert provider.list_subtitles(video, {Language('zho')}) == []
    video.episode, video.season = 2, 2
    requests_mock.get(BASE + '/search/Three-Body%20S02', text=search_html('zYrQnc'))
    assert provider.list_subtitles(video, {Language('zho')}) == []


def test_pack_without_season_in_release_uses_title_search(provider, requests_mock):
    video = Episode('Three-Body.S01E02.mkv', 'Three-Body', 1, 2, series_imdb_id='tt20242042')
    requests_mock.get(BASE + '/search/Three-Body%20S01', text='<html></html>')
    requests_mock.get(BASE + '/search/Three-Body', text=search_html('zYrQnc'))
    requests_mock.get(BASE + '/a/zYrQnc', text=(DATA / 'pack.html').read_text())
    subs = provider.list_subtitles(video, {Language('zho')})
    assert len(subs) == 1
    assert 'S01E02' in subs[0].filename


@pytest.mark.parametrize('filename', ['Three-Body.S02E02.chs.srt', 'Three-Body.S01E01-E02.chs.srt'])
def test_file_episode_takes_precedence_over_release(provider, requests_mock, filename):
    video = Episode('Three-Body.S01E02.mkv', 'Three-Body', 1, 2, series_imdb_id='tt20242042')
    requests_mock.get(BASE + '/search/Three-Body%20S01', text=search_html('pack'))
    requests_mock.get(BASE + '/search/Three-Body', text=search_html('pack'))
    requests_mock.get(BASE + '/a/pack', text=f'''<h1>Three-Body (2023)</h1>
        <div class="subtitle-edition">Three-Body.S01E02</div>
        <div class="subtitle-metadata-tags"><span>简体</span></div>
        <a data-filename="{filename}"></a>''')
    assert provider.list_subtitles(video, {Language('zho')}) == []


def test_separate_english_and_unidentified_files_are_excluded(provider, movie, requests_mock):
    detail = (DATA / 'movie.html').read_text().replace(
        'data-filename="1774886230488.ass"', 'data-filename="Movie.chs.ass"')
    detail += '<a data-filename="Movie.eng.srt"></a><a data-filename="unknown.srt"></a>'
    requests_mock.get(BASE + '/search/Interstellar', text=search_html())
    requests_mock.get(BASE + '/a/Cm0tsS', text=detail)
    subs = provider.list_subtitles(movie, {Language('zho')})
    assert [sub.filename for sub in subs] == ['Movie.chs.ass']


def test_search_query_is_url_encoded(provider, requests_mock):
    requests_mock.get(BASE + '/search/A%2FB%20%26%20C', text='<html></html>')
    assert list(provider._search('A/B & C', {Language('zho')})) == []


def test_download_uses_fresh_ticket_and_session(provider, requests_mock):
    register_download(requests_mock)
    sub = subtitle()
    provider.download_subtitle(sub)
    assert sub.content == SRT.replace(b'\r\n', b'\n')
    history = requests_mock.request_history
    assert history[1].json() == {'sid': 'Cm0tsS'}
    assert history[1].headers['Sec-Fetch-Site'] == 'same-origin'
    assert history[3].headers['Referer'] == BASE + '/down/Cm0tsS?ticket=fresh'
    assert history[3].json() == {'sid': 'Cm0tsS'}


@pytest.mark.parametrize('member', ['folder/Show.S01E02.chs.SRT', 'Show.S01E02.chs.SRT'])
def test_download_extracts_selected_file_not_first(provider, requests_mock, member):
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('Show.S01E01.chs.SRT', b'Wrong episode')
        archive.writestr('Show.S01E02.eng.SRT', b'Wrong language')
        archive.writestr(member, SRT)
    register_download(requests_mock, stream.getvalue(), 'zip')
    sub = subtitle('folder/Show.S01E02.chs.SRT', Language('zho'))
    sub.is_pack = True
    provider.download_subtitle(sub)
    assert sub.content == SRT.replace(b'\r\n', b'\n')


@pytest.mark.skipif(not shutil.which('unrar'), reason='Requires the unrar binary used by Bazarr')
def test_download_rar_pack(provider, requests_mock):
    register_download(requests_mock, (DATA.parent / 'archive_2.rar').read_bytes(), 'rar')
    sub = subtitle('101 - Pilot.srt')
    sub.is_pack = True
    provider.download_subtitle(sub)
    assert sub.content.startswith(b'1\n00:01:09,069 --> 00:01:10,900\n')
    assert len(sub.content) == 39180


@pytest.mark.parametrize('members', [['wrong.srt'], ['a/test.srt', 'b/test.srt']])
def test_download_rejects_missing_or_ambiguous_member(provider, requests_mock, members):
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        for member in members:
            archive.writestr(member, SRT)
    register_download(requests_mock, stream.getvalue(), 'zip')
    with pytest.raises(ProviderError, match='unambiguously'):
        provider.download_subtitle(subtitle())


@pytest.mark.parametrize('body,extension', [(b'<html>Blocked</html>', 'srt'), (b'', 'srt'), (b'7z...', '7z')])
def test_download_rejects_error_pages_and_unsupported_formats(provider, requests_mock, body, extension):
    register_download(requests_mock, body, extension)
    with pytest.raises(ProviderError):
        provider.download_subtitle(subtitle())


@pytest.mark.parametrize('result', [
    {'success': False}, {'success': True, 'pass': False},
    {'success': True, 'pass': True, 'url': 'file:///etc/passwd'},
    {'success': True, 'pass': True}, [],
])
def test_download_refusal(provider, requests_mock, result):
    register_download(requests_mock)
    requests_mock.post(BASE + '/api/sub/down', json=result)
    sub = subtitle()
    with pytest.raises(ProviderError):
        provider.download_subtitle(sub)
    assert sub.content is None


def test_download_rejects_single_file_for_pack(provider, requests_mock):
    register_download(requests_mock)
    sub = subtitle()
    sub.is_pack = True
    with pytest.raises(ProviderError, match='single file'):
        provider.download_subtitle(sub)


def test_invalid_json(provider, requests_mock):
    register_download(requests_mock)
    requests_mock.post(BASE + '/api/sub/prepare-download', text='<html>Error</html>')
    with pytest.raises(ProviderError, match='invalid JSON'):
        provider.download_subtitle(subtitle())


@pytest.mark.parametrize('url', ['https://elsewhere.test/down/test', '//elsewhere.test/down/test', None])
def test_invalid_ticket_url(provider, requests_mock, url):
    register_download(requests_mock)
    requests_mock.post(BASE + '/api/sub/prepare-download', json={'success': True, 'url': url})
    with pytest.raises(ProviderError, match='invalid download page'):
        provider.download_subtitle(subtitle())


@pytest.mark.parametrize('status,error', [(403, ForbiddenError), (429, TooManyRequests)])
def test_http_errors_are_reported(provider, requests_mock, status, error):
    requests_mock.get(BASE + '/search/Interstellar', status_code=status)
    with pytest.raises(error):
        list(provider._search('Interstellar', {Language('zho')}))


def test_certificate_verification_is_enabled(provider):
    assert provider.session.verify is not False


def test_subtitle_matching_does_not_accumulate(movie):
    sub = subtitle()
    assert 'imdb_id' in sub.get_matches(movie)
    other = Movie('Other.2020.mkv', 'Other', year=2020, imdb_id='tt9999999')
    assert 'imdb_id' not in sub.get_matches(other)
    assert 'title' not in sub.get_matches(other)
