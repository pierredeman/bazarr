import posixpath
import re

import requests
from bs4 import BeautifulSoup
from guessit import guessit
from subliminal.exceptions import ProviderError
from subliminal.subtitle import fix_line_ending
from subliminal.video import Episode, Movie
from subliminal_patch.providers import Provider
from subliminal_patch.providers.utils import get_archive_from_bytes
from subliminal_patch.subtitle import Subtitle, guess_matches
from subzero.language import Language
from urllib.parse import quote, urljoin, urlparse

LANGUAGE_LABELS = {
    Language('zho'): '简体',
    Language('zho', 'CN'): '简体',
    Language('zho', 'TW'): '繁体',
}
SUBTITLE_EXTENSIONS = ('.srt', '.ass', '.ssa', '.vtt')
FILE_LANGUAGE_PATTERNS = {
    '简体': r'简|簡|(?<![a-z])(?:chs|sc|zh|zhs|zh-cn|zh-hans|hans|gb|cn)(?![a-z])',
    '繁体': r'繁|(?<![a-z])(?:cht|tc|zht|zh-tw|zh-hant|hant|big5)(?![a-z])',
    '英语': r'英|(?<![a-z])(?:en|eng|english)(?![a-z])',
}


def _normalize_filename(filename):
    return posixpath.normpath(filename.replace('\\', '/'))


class SubHDClient:
    """Adapt SubHD responses and handle HTTP requests and cookies."""

    base_url = 'https://subhd.tv'

    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'Mozilla/5.0'

    def close(self):
        self.session.close()

    def search(self, search_text: str):
        """Search movies and series by title or IMDb ID.

        :param str search_text: Free-text title (e.g. 'Interstellar')
            or IMDb ID (e.g. 'tt0816692').
        :return: List of dictionaries containing id, title, title2, year and type.
        """
        url = self.base_url + '/searchD/' + quote(search_text, safe='')
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data['items']:
            results.append({
                'id': item['id'],
                'title': item['title'],
                'title2': item['title2'],
                'year': item['year'],
                'type': item['type'],
            })

        return results

    def list_subtitles(self, subhd_id: str) -> list[dict]:
        """Return subtitle files announced in each publication's detail page.

        :return: List of dictionaries containing id, title, page_link, filename
            and languages as normalized Language objects.
        """
        url = self.base_url + '/d/' + quote(subhd_id, safe='')
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        subtitles = []
        for subtitle_page in self._extract_subtitles(response.text):
            subtitles.extend(self._list_subtitle_files(subtitle_page))

        return subtitles

    def _extract_subtitles(self, html: str) -> list[dict]:
        """Extract publication information and normalize SubHD language labels."""
        soup = BeautifulSoup(html, 'html.parser')
        subtitles = []

        for row in soup.select('.row.pt-2.mb-2'):
            link = row.select_one('.view-text > a[href^="/a/"]')
            if link is None:
                continue

            language_labels = [tag.get_text(strip=True) for tag in row.select('span.p-1.fw-bold')]
            subtitle_languages = {
                language for language, label in LANGUAGE_LABELS.items()
                if label in language_labels
            }
            if not subtitle_languages:
                continue

            subtitles.append({
                'id': link['href'].rsplit('/', 1)[-1],
                'title': link.get_text(strip=True),
                'page_link': urljoin(self.base_url, link['href']),
                'languages': subtitle_languages,
            })

        return subtitles

    def _list_subtitle_files(self, subtitle_page: dict) -> list[dict]:
        """Fetch the publication's announced filenames and their Chinese languages."""
        response = self.session.get(subtitle_page['page_link'], timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        filenames = list(dict.fromkeys(
            tag['data-filename'] for tag in soup.select('[data-filename]')
            if tag['data-filename'].lower().endswith(SUBTITLE_EXTENSIONS)
        ))
        language_labels = {
            tag.get_text(strip=True) for tag in soup.select('.subtitle-metadata-tags span')
            if tag.get_text(strip=True) in FILE_LANGUAGE_PATTERNS
        }

        subtitles = []
        for filename in filenames:
            languages = self._file_languages(filename)
            if not languages:
                if len(filenames) != 1 and len(language_labels) != 1:
                    continue
                languages = subtitle_page['languages']
            languages = languages & LANGUAGE_LABELS.keys()
            if languages:
                subtitles.append({**subtitle_page, 'filename': filename, 'languages': languages})

        return subtitles

    def _file_languages(self, filename: str) -> set[Language]:
        """Read language markers; return an empty set when the filename has none."""
        labels = {
            label for label, pattern in FILE_LANGUAGE_PATTERNS.items()
            if re.search(pattern, filename, re.IGNORECASE)
        }
        if re.search(r'zh[-_.]?(?:tw|hant)', filename, re.IGNORECASE):
            labels.discard('简体')
            labels.add('繁体')

        languages = {language for language, label in LANGUAGE_LABELS.items() if label in labels}
        if '英语' in labels:
            languages.add(Language('eng'))
        return languages

    def download(self, subtitle_id: str):
        """Return the download URL and file bytes for a subtitle publication."""
        payload = {'sid': subtitle_id}
        response = self.session.post(
            self.base_url + '/api/sub/prepare-download', json=payload, timeout=30
        )
        response.raise_for_status()
        prepared = response.json()
        if prepared.get('success') is not True:
            raise ProviderError(prepared.get('msg') or 'SubHD could not prepare the download')

        download_page = urljoin(self.base_url, prepared['url'])
        # Obtain the download cookie without fetching the page body.
        response = self.session.head(download_page, timeout=30)
        response.raise_for_status()

        response = self.session.post(
            self.base_url + '/api/sub/down', json=payload, timeout=30
        )
        response.raise_for_status()
        download = response.json()
        if download.get('success') is not True or download.get('pass') is not True:
            raise ProviderError(download.get('msg') or 'SubHD refused the download')

        download_url = download['url']
        response = self.session.get(download_url, timeout=30)
        response.raise_for_status()
        return download_url, response.content


class SubHDSubtitle(Subtitle):
    """A SubHD subtitle candidate for a file and language."""

    provider_name = 'subhd'

    def __init__(self, language, subtitle_id, title, page_link, filename):
        super().__init__(language, page_link=page_link)
        self.subtitle_id = subtitle_id
        self.filename = filename
        self.release_info = title
        self.extra_release_info = [filename]

    @property
    def id(self):
        return f'{self.subtitle_id}/{self.filename}/{self.language}'

    def get_matches(self, video):
        """Compare the release information with the movie."""
        guess = guessit(self.release_info, {'type': 'movie'})
        return guess_matches(video, guess)


class SubHDProvider(Provider):
    languages = set(LANGUAGE_LABELS)
    video_types = (Movie,)
    subtitle_class = SubHDSubtitle

    def __init__(self):
        self.client = None

    def initialize(self):
        self.client = SubHDClient()

    def list_subtitles(self, video, languages):
        if isinstance(video, Episode):
            raise NotImplementedError('SubHD episode support is not implemented yet')

        return self._list_movie_subtitles(video, languages)

    def download_subtitle(self, subtitle):
        """Download the publication and extract the selected subtitle file."""
        download_url, content = self.client.download(subtitle.subtitle_id)

        if urlparse(download_url).path.lower().endswith(SUBTITLE_EXTENSIONS):
            subtitle.content = fix_line_ending(content)
            return

        archive = get_archive_from_bytes(content)
        if archive is None:
            raise ProviderError('SubHD returned an unsupported download')

        with archive:
            selected_path = _normalize_filename(subtitle.filename)
            matching_files = [
                name for name in archive.namelist()
                if _normalize_filename(name) == selected_path
            ]
            if not matching_files:
                matching_files = [
                    name for name in archive.namelist()
                    if posixpath.basename(_normalize_filename(name)) == posixpath.basename(selected_path)
                ]
            if len(matching_files) != 1:
                raise ProviderError('SubHD archive does not contain the selected subtitle unambiguously')

            subtitle.content = fix_line_ending(archive.read(matching_files[0]))

    def terminate(self):
        self.client.close()

    def _list_movie_subtitles(self, movie: Movie, languages):
        subtitles = []

        for result in self._search_movie(movie):
            for subtitle in self.client.list_subtitles(result['id']):
                for language in languages & subtitle['languages']:
                    subtitles.append(SubHDSubtitle(
                        language=language,
                        subtitle_id=subtitle['id'],
                        title=subtitle['title'],
                        page_link=subtitle['page_link'],
                        filename=subtitle['filename'],
                    ))

        return subtitles

    def _search_episode(self, episode: Episode):
        search_text = episode.series_imdb_id or episode.series
        return self.client.search(search_text)

    def _search_movie(self, movie: Movie):
        search_text = movie.imdb_id or movie.title
        return self.client.search(search_text)
