# -*- coding: utf-8 -*-
import io
import posixpath
import re
from urllib.parse import quote, urljoin, urlparse
from zipfile import ZipFile, is_zipfile

from guessit import guessit
from rarfile import RarFile, is_rarfile
from requests import Session
from subliminal.exceptions import ProviderError
from subliminal.providers import ParserBeautifulSoup
from subliminal.subtitle import fix_line_ending, sanitize
from subliminal.video import Episode, Movie
from subzero.language import Language

from subliminal_patch.exceptions import ForbiddenError, TooManyRequests
from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle, guess_matches

LANGUAGES = {
    '简体': Language('zho'),
    '簡體': Language('zho'),
    '繁体': Language('zho', 'TW'),
    '繁體': Language('zho', 'TW'),
    '英语': Language('eng'),
    '英語': Language('eng'),
}
SUBTITLE_EXTENSIONS = ('.srt', '.ass', '.ssa', '.vtt')
SUBTITLE_PATH = re.compile(r'^/a/([A-Za-z0-9]+)$')
IMDB_LINK = re.compile(r'^https?://(?:www\.)?imdb\.com/title/(tt\d+)')
CHINESE_PREFIX = re.compile(r'^[\u3400-\u9fff]+[.\s_:-]+')


def _file_languages(filename):
    text = filename.lower()
    languages = set()
    for pattern, language in (
        (r'简|簡|(?<![a-z])(?:chs|sc|zh|zhs|zh-cn|zh-hans|hans|gb|cn)(?![a-z])', Language('zho')),
        (r'繁|(?<![a-z])(?:cht|tc|zht|zh-tw|zh-hant|hant|big5)(?![a-z])', Language('zho', 'TW')),
        (r'英|(?<![a-z])(?:en|eng|english)(?![a-z])', Language('eng')),
    ):
        if re.search(pattern, text):
            languages.add(language)
    # A regional tag must not also be interpreted as generic "zh".
    if re.search(r'zh[-_.]?(?:tw|hant)', text):
        languages.discard(Language('zho'))
        languages.add(Language('zho', 'TW'))
    return languages


def _normalize_filename(filename):
    return posixpath.normpath(filename.replace('\\', '/')).lstrip('./')


class SubHDSubtitle(Subtitle):
    provider_name = 'subhd'

    def __init__(self, language, subtitle_id, filename, release, titles, imdb_id, year, video_type):
        super().__init__(language, page_link=f'https://subhd.tv/a/{subtitle_id}')
        self.subtitle_id = subtitle_id
        self.filename = filename
        self.release_info = f'{release}\n{filename}'
        self.titles = titles
        self.imdb_id = imdb_id
        self.year = year
        options = {'type': video_type}
        self.guess = dict(guessit(CHINESE_PREFIX.sub('', release), options))
        # File metadata takes precedence over the enclosing season pack.
        basename = posixpath.basename(_normalize_filename(filename))
        self.guess.update(guessit(CHINESE_PREFIX.sub('', basename), options))

    @property
    def id(self):
        return f'{self.subtitle_id}/{self.filename}/{self.language}'

    def get_matches(self, video):
        matches = guess_matches(video, self.guess)
        if isinstance(video, Episode):
            title_match = 'series'
            imdb_match = 'series_imdb_id'
            titles = [video.series] + video.alternative_series
            imdb_id = video.series_imdb_id
        else:
            title_match = 'title'
            imdb_match = 'imdb_id'
            titles = [video.title] + video.alternative_titles
            imdb_id = video.imdb_id

        video_titles = {sanitize(title) for title in titles if title}
        subtitle_titles = {sanitize(title) for title in self.titles if title}
        if video_titles & subtitle_titles:
            matches.add(title_match)
        if imdb_id and imdb_id == self.imdb_id:
            matches.update((title_match, imdb_match))
        if video.year and video.year == self.year:
            matches.add('year')

        self.matches = matches
        return matches


class SubHDProvider(Provider):
    subtitle_class = SubHDSubtitle
    languages = {Language('zho'), Language('zho', 'CN'), Language('zho', 'TW')}
    video_types = (Episode, Movie)
    server_url = 'https://subhd.tv'

    def initialize(self):
        self.session = Session()
        self.session.headers['User-Agent'] = 'Mozilla/5.0 (compatible; Bazarr)'

    def terminate(self):
        self.session.close()

    def list_subtitles(self, video, languages):
        requested_languages = set(languages) & self.languages
        if not requested_languages:
            return []

        search_languages = {
            Language('zho') if language == Language('zho', 'CN') else language
            for language in requested_languages
        }
        seen_ids = set()
        subtitles = []

        for query in self._search_queries(video):
            for subtitle_id in self._search(query, search_languages):
                if subtitle_id in seen_ids:
                    continue
                seen_ids.add(subtitle_id)

                details = self._detail(subtitle_id)
                subtitles.extend(self._subtitles_from_detail(
                    video, requested_languages, subtitle_id, details
                ))

            if subtitles:
                break

        return subtitles

    def download_subtitle(self, subtitle):
        # Always obtain a fresh ticket: download pages expire and are session-bound.
        self._get(subtitle.page_link)
        prepared = self._post('/api/sub/prepare-download', subtitle.subtitle_id, subtitle.page_link)
        download_path = prepared.get('url')
        if not isinstance(download_path, str) or not download_path.startswith('/down/'):
            raise ProviderError('SubHD returned an invalid download page')

        download_page = urljoin(self.server_url, download_path)
        self._get(download_page, headers={'Referer': subtitle.page_link})
        result = self._post('/api/sub/down', subtitle.subtitle_id, download_page)
        download_url = result.get('url')
        if result.get('pass') is not True or not isinstance(download_url, str):
            raise ProviderError('SubHD returned an invalid download URL')
        if urlparse(download_url).scheme != 'https':
            raise ProviderError('SubHD returned an invalid download URL')

        content = self._get(download_url, headers={'Referer': download_page}).content
        stream = io.BytesIO(content)
        if is_zipfile(stream):
            archive = ZipFile(stream)
        elif is_rarfile(stream):
            archive = RarFile(stream)
        else:
            if subtitle.is_pack:
                raise ProviderError('SubHD returned a single file for a subtitle pack')

            extension = posixpath.splitext(urlparse(download_url).path)[1].lower()
            if extension not in SUBTITLE_EXTENSIONS:
                raise ProviderError('SubHD returned an unsupported subtitle archive')
            if not content or content.lstrip().lower().startswith((b'<!doctype html', b'<html', b'{')):
                raise ProviderError('SubHD returned an empty subtitle or an error page')

            subtitle.content = fix_line_ending(content)
            return

        with archive:
            selected_path = _normalize_filename(subtitle.filename)
            matching_names = [
                name for name in archive.namelist()
                if _normalize_filename(name) == selected_path
            ]
            if not matching_names:
                selected_basename = posixpath.basename(selected_path)
                matching_names = [
                    name for name in archive.namelist()
                    if posixpath.basename(_normalize_filename(name)) == selected_basename
                ]
            if len(matching_names) != 1:
                raise ProviderError('SubHD archive does not contain the selected subtitle unambiguously')

            subtitle.content = fix_line_ending(archive.read(matching_names[0]))

    @staticmethod
    def _checked(response):
        if response.status_code == 429:
            raise TooManyRequests('SubHD request limit reached')
        if response.status_code == 403:
            raise ForbiddenError('SubHD refused the request')
        response.raise_for_status()
        return response

    def _get(self, url, **kwargs):
        response = self.session.get(url, timeout=30, **kwargs)
        return self._checked(response)

    def _post(self, path, subtitle_id, referer):
        headers = {
            'Referer': referer,
            'Origin': self.server_url,
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'cors',
        }
        response = self.session.post(
            self.server_url + path,
            json={'sid': subtitle_id},
            timeout=30,
            headers=headers,
        )
        self._checked(response)

        try:
            data = response.json()
        except ValueError as error:
            raise ProviderError('SubHD returned invalid JSON') from error
        if not isinstance(data, dict) or data.get('success') is not True:
            raise ProviderError('SubHD could not prepare the subtitle download')
        if data.get('pass') is False:
            raise ProviderError('SubHD requires browser verification before downloading')
        return data

    @staticmethod
    def _soup(response):
        return ParserBeautifulSoup(response.content.decode('utf-8'), ['lxml', 'html.parser'])

    @staticmethod
    def _search_queries(video):
        is_episode = isinstance(video, Episode)
        if is_episode:
            titles = [video.series] + video.alternative_series
        else:
            titles = [video.title] + video.alternative_titles

        queries = []
        for title in titles:
            if not title:
                continue
            if is_episode:
                queries.append(f'{title} S{video.season:02d}')
            # Some packs only mention their season in the individual filenames.
            queries.append(title)

        return list(dict.fromkeys(queries))

    def _search(self, title, languages):
        path = '/search/' + quote(title, safe='')
        soup = self._soup(self._get(self.server_url + path))
        for card in soup.select('div.bg-white.shadow-sm.rounded-3.mb-4'):
            link = card.select_one('.view-text a[href]')
            if not link:
                continue
            subtitle_match = SUBTITLE_PATH.fullmatch(link['href'])
            if not subtitle_match:
                continue

            available_languages = set()
            for tag in card.select('span.fw-bold'):
                language = LANGUAGES.get(tag.get_text(strip=True))
                if language:
                    available_languages.add(language)

            if available_languages & languages:
                yield subtitle_match.group(1)

    def _detail(self, subtitle_id):
        response = self._get(f'{self.server_url}/a/{subtitle_id}')
        soup = self._soup(response)
        edition = soup.select_one('.subtitle-edition')
        metadata_tags = soup.select_one('.subtitle-metadata-tags')
        if edition is None or metadata_tags is None:
            raise ProviderError('SubHD subtitle details are missing')

        files = [tag['data-filename'] for tag in soup.select('[data-filename]')]
        languages = set()
        for tag in metadata_tags.select('span'):
            language = LANGUAGES.get(tag.get_text(strip=True))
            if language:
                languages.add(language)

        titles = []
        for tag in soup.find_all('b'):
            if tag.get_text(strip=True) in ('Title', '名称') and isinstance(tag.next_sibling, str):
                titles.append(tag.next_sibling.strip().lstrip('：:').strip())

        year = None
        heading = soup.select_one('h1')
        if heading:
            year_match = re.search(r'\((\d{4})\)', heading.get_text())
            if year_match:
                year = int(year_match.group(1))
        imdb_id = None
        imdb_link = soup.find('a', href=IMDB_LINK)
        if imdb_link:
            imdb_id = IMDB_LINK.match(imdb_link['href']).group(1)

        return {
            'release': edition.get_text(' ', strip=True),
            'titles': titles,
            'imdb_id': imdb_id,
            'year': year,
            'languages': languages,
            'files': list(dict.fromkeys(files)),
        }

    def _subtitles_from_detail(self, video, requested_languages, subtitle_id, details):
        is_episode = isinstance(video, Episode)
        if is_episode:
            video_type = 'episode'
        else:
            video_type = 'movie'
            if video.imdb_id and details['imdb_id'] and video.imdb_id != details['imdb_id']:
                return []
            if video.year and details['year'] and video.year != details['year']:
                return []

        subtitles = []
        for filename in details['files']:
            if not filename.lower().endswith(SUBTITLE_EXTENSIONS):
                continue

            available_languages = _file_languages(filename)
            if not available_languages:
                single_file = len(details['files']) == 1
                single_language = len(details['languages']) == 1
                if single_file or single_language:
                    available_languages = details['languages']

            for language in requested_languages:
                normalized_language = language
                if language == Language('zho', 'CN'):
                    normalized_language = Language('zho')
                if normalized_language not in available_languages:
                    continue

                subtitle = self.subtitle_class(
                    language=language,
                    subtitle_id=subtitle_id,
                    filename=filename,
                    release=details['release'],
                    titles=details['titles'],
                    imdb_id=details['imdb_id'],
                    year=details['year'],
                    video_type=video_type,
                )
                subtitle.is_pack = len(details['files']) > 1
                matches = subtitle.get_matches(video)
                if is_episode:
                    if subtitle.guess.get('season') != video.season:
                        continue
                    if subtitle.guess.get('episode') != video.episode:
                        continue
                    if 'series' not in matches:
                        continue
                elif 'title' not in matches:
                    continue

                subtitles.append(subtitle)

        return subtitles
