import io
from zipfile import ZipFile

import pytest
from subliminal.exceptions import ProviderError
from subliminal.video import Episode, Movie
from subliminal_patch.extensions import provider_registry
from subliminal_patch.subhd2 import SubHD2Provider, SubHD2Subtitle
from subzero.language import Language


BASE = 'https://subhd.tv'
SRT = b'1\r\n00:00:01,000 --> 00:00:02,000\r\nSelected subtitle\r\n'


def subtitle(filename='Interstellar.chs.srt'):
    return SubHD2Subtitle(Language('zho'), 'publication', 'Interstellar.2014',
                          BASE + '/a/publication', filename)


def register_download(requests_mock, content, extension='srt'):
    requests_mock.post(BASE + '/api/sub/prepare-download',
                       json={'success': True, 'url': '/down/publication?ticket=fresh'})
    requests_mock.head(BASE + '/down/publication?ticket=fresh')
    requests_mock.post(BASE + '/api/sub/down',
                       json={'success': True, 'pass': True, 'url': f'https://dl.subhd.me/test.{extension}'})
    requests_mock.get(f'https://dl.subhd.me/test.{extension}', content=content)


def test_registered_provider_supports_chinese_movies_only():
    provider = provider_registry['subhd2']
    movie = Movie('movie.mkv', 'Interstellar')
    episode = Episode('episode.mkv', 'Series', 1, 1)
    assert provider is SubHD2Provider
    assert provider.subtitle_class is SubHD2Subtitle
    assert provider.languages == {Language('zho'), Language('zho', 'CN'), Language('zho', 'TW')}
    assert provider.check(movie)
    assert not provider.check(episode)
    with pytest.raises(NotImplementedError):
        provider().list_subtitles(episode, {Language('zho')})


def test_movie_search_returns_candidates_for_each_announced_chinese_file(requests_mock):
    movie = Movie('movie.mkv', 'Interstellar', imdb_id='tt0816692')
    requests_mock.get(BASE + '/searchD/tt0816692', json={'items': [
        {'id': 'movie', 'title': 'Interstellar', 'title2': '', 'year': 2014, 'type': 'movie'}
    ]})
    requests_mock.get(BASE + '/d/movie', text='''
        <div class="row pt-2 mb-2">
          <div class="view-text"><a href="/a/publication">Interstellar.2014</a></div>
          <span class="p-1 fw-bold">简体</span><span class="p-1 fw-bold">英语</span>
        </div>''')
    requests_mock.get(BASE + '/a/publication', text='''
        <div data-filename="README.txt"></div>
        <div data-filename="Interstellar.chs.srt"></div>
        <div data-filename="Interstellar.chs.eng.ass"></div>
        <div data-filename="Interstellar.eng.srt"></div>''')

    with SubHD2Provider() as provider:
        results = provider.list_subtitles(movie, {Language('zho'), Language('eng')})

    assert [result.filename for result in results] == ['Interstellar.chs.srt', 'Interstellar.chs.eng.ass']
    assert {result.language for result in results} == {Language('zho')}
    assert len({result.id for result in results}) == 2
    assert results[1].extra_release_info == ['Interstellar.chs.eng.ass']
    assert [request.method for request in requests_mock.request_history] == ['GET', 'GET', 'GET']


def test_direct_download_uses_minimal_api_pipeline(requests_mock):
    register_download(requests_mock, SRT)
    selected = subtitle()
    with SubHD2Provider() as provider:
        provider.download_subtitle(selected)
    assert selected.content == SRT.replace(b'\r\n', b'\n')
    assert [request.method for request in requests_mock.request_history] == ['POST', 'HEAD', 'POST', 'GET']


@pytest.mark.parametrize('selected_name,archive_names', [
    ('folder/Interstellar.chs.srt', ['folder/Interstellar.chs.srt', 'other/Interstellar.chs.srt']),
    ('folder/Interstellar.chs.srt', ['folder\\Interstellar.chs.srt']),
    ('Interstellar.chs.srt', ['folder/Interstellar.chs.srt']),
])
def test_archive_download_extracts_the_selected_file(requests_mock, selected_name, archive_names):
    data = io.BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('README.txt', b'Instructions')
        for index, name in enumerate(archive_names):
            archive.writestr(name, SRT if index == 0 else b'Wrong subtitle')
        archive.writestr('Interstellar.eng.srt', b'English subtitle')
    register_download(requests_mock, data.getvalue(), 'zip')
    selected = subtitle(selected_name)
    with SubHD2Provider() as provider:
        provider.download_subtitle(selected)
    assert selected.content == SRT.replace(b'\r\n', b'\n')


def test_archive_download_rejects_an_ambiguous_basename(requests_mock):
    data = io.BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('one/Interstellar.chs.srt', SRT)
        archive.writestr('two/Interstellar.chs.srt', SRT)
    register_download(requests_mock, data.getvalue(), 'zip')
    with SubHD2Provider() as provider, pytest.raises(ProviderError, match='unambiguously'):
        provider.download_subtitle(subtitle())
