# -*- coding: utf-8 -*-
# Manage Youtube subscriptions using youtube-dl (https://github.com/ytdl-org/youtube-dl)
# Requirements: youtube-dl module (pip install youtube_dl)
# (c) 2019-08-17 Eric Le Lay <elelay.fr:contact>
# Released under the same license terms as gPodder itself.

import logging
import os
import re
import time

import youtube_dl
from youtube_dl.utils import DownloadError, sanitize_url

import gpodder
from gpodder import download, feedcore, model, registry, youtube
from gpodder.util import mimetype_from_extension, remove_html_tags

_ = gpodder.gettext


logger = logging.getLogger(__name__)


__title__ = 'Youtube-dl'
__description__ = _('Manage Youtube subscriptions using youtube-dl (pip install youtube_dl)')
__only_for__ = 'gtk, cli'
__authors__ = 'Eric Le Lay <elelay.fr:contact>'
__doc__ = 'https://gpodder.github.io/docs/extensions/youtubedl.html'

DefaultConfig = {
    # youtube-dl downloads and parses each video page to get informations about it, which is very slow.
    # Set to False to fall back to the fast but limited (only 15 episodes) gpodder code
    'manage_channel': True,
    # If for some reason youtube-dl download doesn't work for you, you can fallback to gpodder code.
    # Set to False to fall back to default gpodder code (less available formats).
    'manage_downloads': True,
}


# youtube feed still preprocessed by youtube.py (compat)
CHANNEL_RE = re.compile(r'''https://www.youtube.com/feeds/videos.xml\?channel_id=(.+)''')
PLAYLIST_RE = re.compile(r'''https://www.youtube.com/feeds/videos.xml\?playlist_id=(.+)''')


def youtube_parsedate(s):
    """Parse a string into a unix timestamp

    Only strings provided by Youtube-dl API are
    parsed with this function (20170920).
    """
    if s:
        return time.mktime(time.strptime(s, "%Y%m%d"))
    return 0


def video_guid(video_id):
    """
    generate same guid as youtube
    """
    return 'yt:video:{}'.format(video_id)


class YoutubeCustomDownload(download.CustomDownload):
    """
    Represents the download of a single episode using youtube-dl.

    Actual youtube-dl interaction via gPodderYoutubeDL.
    """
    def __init__(self, ytdl, url):
        self._ytdl = ytdl
        self._url = url
        self._reporthook = None
        self._prev_dl_bytes = 0

    def retrieve_resume(self, tempname, reporthook=None):
        """
        called by download.DownloadTask to perform the download.
        """
        self._reporthook = reporthook
        res = self._ytdl.fetch_video(self._url, tempname, self._my_hook)
        headers = {}
        # youtube-dl doesn't return a content-type but an extension
        if 'ext' in res:
            dot_ext = '.{}'.format(res['ext'])
            ext_filetype = mimetype_from_extension(dot_ext)
            if ext_filetype:
                headers['content-type'] = ext_filetype
            # See #673 when merging multiple formats, the extension is appended to the tempname
            # by YoutubeDL resulting in empty .partial file + .partial.mp4 exists
            tempstat = os.stat(tempname)
            if not tempstat.st_size:
                tempname_with_ext = tempname + dot_ext
                if os.path.isfile(tempname_with_ext):
                    logger.debug('Youtubedl downloaded to %s instead of %s, moving',
                                 os.path.basename(tempname),
                                 os.path.basename(tempname_with_ext))
                    os.remove(tempname)
                    os.rename(tempname_with_ext, tempname)
        return headers, res.get('url', self._url)

    def _my_hook(self, d):
        if d['status'] == 'downloading':
            if self._reporthook:
                dl_bytes = d['downloaded_bytes']
                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                self._reporthook(self._prev_dl_bytes + dl_bytes,
                                 1,
                                 self._prev_dl_bytes + total_bytes)
        elif d['status'] == 'finished':
            dl_bytes = d['downloaded_bytes']
            self._prev_dl_bytes += dl_bytes
            if self._reporthook:
                self._reporthook(self._prev_dl_bytes, 1, self._prev_dl_bytes)
        elif d['status'] == 'error':
            logger.error('download hook error: %r', d)
        else:
            logger.debug('unknown download hook status: %r', d)


class YoutubeFeed(model.Feed):
    """
    Represents the youtube feed for model.PodcastChannel
    """
    def __init__(self, url, cover_url, description, max_episodes, ie_result, downloader):
        self._url = url
        self._cover_url = cover_url
        self._description = description
        self._max_episodes = max_episodes
        ie_result['entries'] = self._process_entries(ie_result.get('entries', []))
        self._ie_result = ie_result
        self._downloader = downloader

    def _process_entries(self, entries):
        filtered_entries = []
        seen_guids = set()
        for i, e in enumerate(entries):  # consumes the generator!
            if e.get('_type', 'video') == 'url' and e.get('ie_key') == 'Youtube':
                guid = video_guid(e['id'])
                e['guid'] = guid
                if guid in seen_guids:
                    logger.debug('dropping already seen entry %s title="%s"', guid, e.get('title'))
                else:
                    filtered_entries.append(e)
                    seen_guids.add(guid)
            else:
                logger.debug('dropping entry not youtube video %r', e)
            if len(filtered_entries) == self._max_episodes:
                # entries is a generator: stopping now prevents it to download more pages
                logger.debug('stopping entry enumeration')
                break
        return filtered_entries

    def get_title(self):
        return '{} (Youtube)'.format(self._ie_result.get('title') or self._ie_result.get('id') or self._url)

    def get_link(self):
        return self._ie_result.get('webpage_url')

    def get_description(self):
        return self._description

    def get_cover_url(self):
        return self._cover_url

    def get_http_etag(self):
        """ :return str: optional -- last HTTP etag header, for conditional request next time """
        # youtube-dl doesn't provide it!
        return None

    def get_http_last_modified(self):
        """ :return str: optional -- last HTTP Last-Modified header, for conditional request next time """
        # youtube-dl doesn't provide it!
        return None

    def get_new_episodes(self, channel, existing_guids):
        # entries are already sorted by decreasing date
        # trim guids to max episodes
        entries = [e for i, e in enumerate(self._ie_result['entries'])
                   if not self._max_episodes or i < self._max_episodes]
        all_seen_guids = set(e['guid'] for e in entries)
        # only fetch new ones from youtube since they are so slow to get
        new_entries = [e for e in entries if e['guid'] not in existing_guids]
        logger.debug('%i/%i new entries', len(new_entries), len(all_seen_guids))
        self._ie_result['entries'] = new_entries
        self._downloader.refresh_entries(self._ie_result, self._max_episodes)
        # episodes from entries
        episodes = []
        for en in self._ie_result['entries']:
            guid = video_guid(en['id'])
            description = remove_html_tags(en.get('description') or _('No description available'))
            html_description = self.nice_html_description(en, description)
            if en.get('ext'):
                mime_type = mimetype_from_extension('.{}'.format(en['ext']))
            else:
                mime_type = 'application/octet-stream'
            if en.get('filesize'):
                filesize = int(en['filesize'] or 0)
            else:
                filesize = sum(int(f.get('filesize') or 0)
                               for f in en.get('requested_formats', []))
            ep = {
                'title': en.get('title', guid),
                'link': en.get('webpage_url'),
                'description': description,
                'description_html': html_description,
                'url': en.get('webpage_url'),
                'file_size': filesize,
                'mime_type': mime_type,
                'guid': guid,
                'published': youtube_parsedate(en.get('upload_date', None)),
                'total_time': int(en.get('duration') or 0),
            }
            episode = channel.episode_factory(ep)
            episode.save()
            episodes.append(episode)
        return episodes, all_seen_guids

    def get_next_page(self, channel, max_episodes):
        """
        Paginated feed support (RFC 5005).
        If the feed is paged, return the next feed page.
        Returned page will in turn be asked for the next page, until None is returned.
        :return feedcore.Result: the next feed's page,
                                 as a fully parsed Feed or None
        """
        return None

    @staticmethod
    def nice_html_description(en, description):
        """
        basic html formating + hyperlink highlighting + video thumbnail
        """
        description = re.sub(r'''https?://[^\s]+''',
                             r'''<a href="\g<0>">\g<0></a>''',
                             description)
        description = description.replace('\n', '<br>')
        html = """<style type="text/css">
        body > img { float: left; max-width: 30vw; margin: 0 1em 1em 0; }
        </style>
        """
        img = en.get('thumbnail')
        if img:
            html += '<img src="{}">'.format(img)
        html += '<p>{}</p>'.format(description)
        return html


class gPodderYoutubeDL(download.CustomDownloader):
    def __init__(self, gpodder_config=None):
        self.gpodder_config = gpodder_config
        # cachedir is not much used in youtube-dl, but set it anyway
        cachedir = os.path.join(gpodder.home, 'youtube-dl')
        os.makedirs(cachedir, exist_ok=True)
        self._ydl_opts = {
            'cachedir': cachedir,
            'no_color': True,  # prevent escape codes in desktop notifications on errors
        }

    def add_format(self, gpodder_config, opts, fallback=None):
        """ construct youtube-dl -f argument from configured format. """
        # You can set a custom format or custom formats by editing the config for key
        # `youtube.preferred_fmt_ids`
        #
        # It takes a list of format strings separated by comma: bestaudio, 18
        # they are translated to youtube dl format bestaudio/18, meaning preferably
        # the best audio quality (audio-only) and MP4 360p if it's not available.
        #
        # See https://github.com/ytdl-org/youtube-dl#format-selection for details
        # about youtube-dl format specification.
        fmt_ids = youtube.get_fmt_ids(gpodder_config.youtube)
        opts['format'] = '/'.join(str(fmt) for fmt in fmt_ids)
        if fallback:
            opts['format'] += '/' + fallback

    def fetch_video(self, url, tempname, reporthook):
        opts = {
            'outtmpl': tempname,  # use given tempname by DownloadTask
            'nopart': True,  # don't append .part (already .partial)
            'retries': 3,  # retry a few times
            'progress_hooks': [reporthook]  # to notify UI
        }
        opts.update(self._ydl_opts)
        self.add_format(self.gpodder_config, opts)
        with youtube_dl.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    def refresh_entries(self, ie_result, max_episodes):
        # only interested in video metadata
        opts = {
            'skip_download': True,  # don't download the video
            'youtube_include_dash_manifest': False,  # don't download the DASH manifest
        }
        self.add_format(self.gpodder_config, opts, fallback='18')
        opts.update(self._ydl_opts)
        try:
            with youtube_dl.YoutubeDL(opts) as ydl:
                ydl.process_ie_result(ie_result, download=False)
        except DownloadError:
            logger.exception('refreshing %r', ie_result)

    def refresh(self, url, channel_url, max_episodes):
        """
        Fetch a channel or playlist contents.

        Doesn't yet fetch video entry informations, so we only get the video id and title.
        """
        # Duplicate a bit of the YoutubeDL machinery here because we only
        # want to parse the channel/playlist first, not to fetch video entries.
        # We call YoutubeDL.extract_info(process=False), so we
        # have to call extract_info again ourselves when we get a result of type 'url'.
        def extract_type(ie_result):
            result_type = ie_result.get('_type', 'video')
            if result_type not in ('url', 'playlist', 'multi_video'):
                raise Exception('Unsuported result_type: {}'.format(result_type))
            has_playlist = result_type in ('playlist', 'multi_video')
            return result_type, has_playlist

        opts = {
            'youtube_include_dash_manifest': False,  # only interested in video title and id
        }
        opts.update(self._ydl_opts)
        with youtube_dl.YoutubeDL(opts) as ydl:
            ie_result = ydl.extract_info(url, download=False, process=False)
            result_type, has_playlist = extract_type(ie_result)
            while not has_playlist:
                if result_type in ('url', 'url_transparent'):
                    ie_result['url'] = sanitize_url(ie_result['url'])
                if result_type == 'url':
                    logger.debug("extract_info(%s) to get the video list", ie_result['url'])
                    # We have to add extra_info to the results because it may be
                    # contained in a playlist
                    ie_result = ydl.extract_info(ie_result['url'],
                                                 download=False,
                                                 process=False,
                                                 ie_key=ie_result.get('ie_key'))
                result_type, has_playlist = extract_type(ie_result)
        cover_url = youtube.get_cover(channel_url)  # youtube-dl doesn't provide the cover url!
        description = youtube.get_channel_desc(channel_url)  # youtube-dl doesn't provide the description!
        return feedcore.Result(feedcore.UPDATED_FEED,
            YoutubeFeed(url, cover_url, description, max_episodes, ie_result, self))

    def fetch_channel(self, channel, max_episodes=0):
        """
        called by model.gPodderFetcher to get a custom feed.
        :returns feedcore.Result: a YoutubeFeed or None if channel is not a youtube channel or playlist
        """
        url = None
        m = CHANNEL_RE.match(channel.url)
        if m:
            url = 'https://www.youtube.com/channel/{}'.format(m.group(1))
        else:
            m = PLAYLIST_RE.match(channel.url)
            if m:
                url = 'https://www.youtube.com/playlist?list={}'.format(m.group(1))
        if url:
            logger.info('Youtube-dl Handling %s => %s', channel.url, url)
            return self.refresh(url, channel.url, max_episodes)
        return None

    def custom_downloader(self, unused_config, episode):
        """
        called from registry.custom_downloader.resolve
        """
        if re.match(r'''https://www.youtube.com/watch\?v=.+''', episode.url):
            return YoutubeCustomDownload(self, episode.url)
        elif re.match(r'''https://www.youtube.com/watch\?v=.+''', episode.link):
            return YoutubeCustomDownload(self, episode.link)
        return None


class gPodderExtension:
    def __init__(self, container):
        self.container = container

    def on_load(self):
        self.ytdl = gPodderYoutubeDL(self.container.manager.core.config)
        logger.info('Registering youtube-dl.')
        if self.container.config.manage_channel:
            registry.feed_handler.register(self.ytdl.fetch_channel)
        if self.container.config.manage_downloads:
            registry.custom_downloader.register(self.ytdl.custom_downloader)

    def on_unload(self):
        logger.info('Unregistering youtube-dl.')
        try:
            registry.feed_handler.unregister(self.ytdl.fetch_channel)
        except ValueError:
            pass
        try:
            registry.custom_downloader.unregister(self.ytdl.custom_downloader)
        except ValueError:
            pass
