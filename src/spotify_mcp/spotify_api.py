import logging
import os
from typing import Optional, Dict, List

import spotipy
from dotenv import load_dotenv
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyOAuth
import requests
from . import utils
from requests import RequestException

load_dotenv()

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
LOCAL_SEARCH_URL = os.getenv("LOCAL_SEARCH_URL")
SPOTIFY_COUNTRY = os.getenv("SPOTIFY_COUNTRY")


# Normalize the redirect URI to meet Spotify's requirements
if REDIRECT_URI:
    REDIRECT_URI = utils.normalize_redirect_uri(REDIRECT_URI)

SCOPES = ["user-read-currently-playing", "user-read-playback-state", "user-read-currently-playing",  # spotify connect
          "app-remote-control", "streaming",  # playback
          "playlist-read-private", "playlist-read-collaborative", "playlist-modify-private", "playlist-modify-public",
          # playlists
          "user-read-playback-position", "user-top-read", "user-read-recently-played",  # listening history
          "user-library-modify", "user-library-read",  # library
          ]


class Client:
    def smart_search(
        self,
        query: str,
        qtype: str = 'track',
        limit: int = 10,
    ) -> dict:
        try:
            local_resp = requests.get(
                LOCAL_SEARCH_URL,
                params={'q': query, 'type': qtype},
                timeout=5
            )
            local_resp.raise_for_status()
            self.logger.info(f"Results: {local_resp.json()}")
            local_data = local_resp.json()
        except RequestException as e:
            self.logger.info(f"[local search failed] {e}")
            local_data = None

        if local_data and isinstance(local_data, list) and len(local_data) > 0:
            documents = local_data.get("documents", [])
            if documents:
                local_results = utils.parse_local_documents(documents, qtype)
                if local_results:
                    if 'tracks' in local_results:
                        local_results['tracks']['items'] = [
                            self.parse_track(t, False) for t in local_results['tracks']['items'][:limit]
                        ]
                        local_results['tracks']['total'] = len(local_results['tracks']['items'])

                    elif 'playlists' in local_results:
                        local_results['playlists']['items'] = [
                            self.parse_playlist(p) for p in local_results['playlists']['items'][:limit]
                        ]
                        local_results['playlists']['total'] = len(local_results['playlists']['items'])

                    return local_results

        self.logger.info("Falling back to online Spotify search")
        online_results = self.sp.search(q=query, type=qtype, limit=limit,market=SPOTIFY_COUNTRY)
        parsed_results = utils.parse_search_results(online_results, qtype, self.username)
        return parsed_results

    def __init__(self, logger: logging.Logger):
        """Initialize Spotify client with necessary permissions"""
        self.logger = logger

        scope = "user-library-read,user-read-playback-state,user-modify-playback-state,user-read-currently-playing,playlist-read-private,playlist-read-collaborative,playlist-modify-private,playlist-modify-public"

        try:
            self.sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
                scope=scope,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                redirect_uri=REDIRECT_URI))

            self.auth_manager: SpotifyOAuth = self.sp.auth_manager
            self.cache_handler: CacheFileHandler = self.auth_manager.cache_handler
        except Exception as e:
            self.logger.error(f"Failed to initialize Spotify client: {str(e)}")
            raise

        self.username = None

    @utils.validate
    def set_username(self, device=None):
        self.username = self.sp.current_user()['display_name']

    def is_valid_track(self, spotify_uri):
        """Validates if a given URI is a valid track in Spotify."""
        try:
            track_id = self._extract_id_from_uri(spotify_uri)
            self.sp.track(track_id)
            return True
        except Exception:
            return False

    def is_valid_playlist(self, spotify_uri):
        """Validates if a given URI is a valid playlist in Spotify."""
        try:
            playlist_id = self._extract_id_from_uri(spotify_uri)
            self.sp.playlist(playlist_id)
            return True
        except Exception:
            return False

    def is_valid_album(self, spotify_uri):
        """Validates if a given URI is a valid album in Spotify."""
        try:
            album_id = self._extract_id_from_uri(spotify_uri)
            self.sp.album(album_id)
            return True
        except Exception:
            return False

    def is_valid_artist(self, spotify_uri):
        """Validates if a given URI is a valid artist in Spotify."""
        try:
            artist_id = self._extract_id_from_uri(spotify_uri)
            self.sp.artist(artist_id)
            return True
        except Exception:
            return False

    def get_playlist_tracks(self, playlist_uri):
        """Gets all tracks from a playlist."""
        playlist_id = self._extract_id_from_uri(playlist_uri)
        results = self.sp.playlist_items(playlist_id)

        tracks = []
        while results:
            for item in results['items']:
                # Some playlist items might be None or not have track info
                if item and 'track' in item and item['track']:
                    track = item['track']
                    track_data = {
                        'uri': track['uri'],
                        'name': track['name'],
                        'artists': [artist['name'] for artist in track['artists']],
                        'duration_ms': track['duration_ms'],
                        'album': track['album']['name'] if 'album' in track else None
                    }
                    tracks.append(track_data)

            # Get next page of results if available
            if results['next']:
                results = self.sp.next(results)
            else:
                results = None

        return tracks

    def get_album_tracks(self, album_uri):
        """Gets all tracks from an album."""
        album_id = self._extract_id_from_uri(album_uri)
        results = self.sp.album_tracks(album_id)

        tracks = []
        while results:
            for track in results['items']:
                track_data = {
                    'uri': track['uri'],
                    'name': track['name'],
                    'artists': [artist['name'] for artist in track['artists']],
                    'duration_ms': track['duration_ms'],
                    'track_number': track['track_number']
                }
                tracks.append(track_data)

            # Get next page of results if available
            if results['next']:
                results = self.sp.next(results)
            else:
                results = None

        return tracks

    def get_artist_top_tracks(self, artist_uri):
        """Gets top tracks from an artist."""
        artist_id = self._extract_id_from_uri(artist_uri)
        # Get market from user's account or default to US
        try:
            user_info = self.sp.current_user()
            market = user_info['country']
        except:
            market = 'US'

        results = self.sp.artist_top_tracks(artist_id, country=market)

        tracks = []
        for track in results['tracks']:
            track_data = {
                'uri': track['uri'],
                'name': track['name'],
                'artists': [artist['name'] for artist in track['artists']],
                'duration_ms': track['duration_ms'],
                'album': track['album']['name'] if 'album' in track else None,
                'popularity': track['popularity']
            }
            tracks.append(track_data)

        return tracks

    def add_to_queue(self, spotify_uri):
        """Adds a track to the queue."""
        # The Spotify API endpoint expects a device_id parameter, but it's optional
        # if the user has an active device
        try:
            self.sp.add_to_queue(spotify_uri)
            return True
        except Exception as e:
            # If no active device is found, try to get one and retry
            devices = self.sp.devices()
            if devices and len(devices['devices']) > 0:
                device_id = devices['devices'][0]['id']
                self.sp.add_to_queue(spotify_uri, device_id=device_id)
                return True
            else:
                raise Exception("No active Spotify device found. Please open Spotify on a device first.")

    def get_queue(self):
        """Gets the current user's queue."""
        try:
            queue = self.sp.queue()
            return queue
        except Exception as e:
            raise Exception(f"Could not retrieve queue: {str(e)}")

    def get_info(self, item_uri):
        """Gets detailed information about a Spotify item based on its URI."""
        item_id = self._extract_id_from_uri(item_uri)

        if 'track' in item_uri:
            item = self.sp.track(item_id)
            info = {
                'type': 'track',
                'name': item['name'],
                'artists': [artist['name'] for artist in item['artists']],
                'album': item['album']['name'],
                'duration_ms': item['duration_ms'],
                'popularity': item['popularity'],
                'uri': item['uri'],
                'external_url': item['external_urls']['spotify'] if 'external_urls' in item else None
            }
        elif 'playlist' in item_uri:
            item = self.sp.playlist(item_id)
            info = {
                'type': 'playlist',
                'name': item['name'],
                'owner': item['owner']['display_name'],
                'description': item['description'],
                'tracks_total': item['tracks']['total'],
                'followers': item['followers']['total'],
                'uri': item['uri'],
                'external_url': item['external_urls']['spotify'] if 'external_urls' in item else None
            }
        elif 'album' in item_uri:
            item = self.sp.album(item_id)
            info = {
                'type': 'album',
                'name': item['name'],
                'artists': [artist['name'] for artist in item['artists']],
                'release_date': item['release_date'],
                'total_tracks': item['total_tracks'],
                'popularity': item['popularity'],
                'uri': item['uri'],
                'external_url': item['external_urls']['spotify'] if 'external_urls' in item else None
            }
        elif 'artist' in item_uri:
            item = self.sp.artist(item_id)
            info = {
                'type': 'artist',
                'name': item['name'],
                'genres': item['genres'],
                'followers': item['followers']['total'],
                'popularity': item['popularity'],
                'uri': item['uri'],
                'external_url': item['external_urls']['spotify'] if 'external_urls' in item else None
            }
        else:
            raise ValueError(f"Unsupported URI type: {item_uri}")

        return info

    def _extract_id_from_uri(self, uri):
        """Extracts the ID portion from a Spotify URI."""
        # Handle different URI formats
        # spotify:type:id
        # https://open.spotify.com/type/id
        if uri.startswith('spotify:'):
            parts = uri.split(':')
            return parts[-1]
        elif uri.startswith('http'):
            # Extract the path and split by '/'
            from urllib.parse import urlparse
            path = urlparse(uri).path
            parts = path.split('/')
            # The ID should be the last part
            return parts[-1]
        else:
            # Assume it's just the ID
            return uri
    @utils.validate
    def search(self, query: str, qtype: str = 'track', limit=10, device=None):
        """
        Searches based on query term.
        - query: query term
        - qtype: the types of items to return. One or more of 'artist', 'album',  'track', 'playlist'.
                If multiple types are desired, pass in a comma separated string; e.g. 'track,album'
        - limit: max # items to return
        """
        if self.username is None:
            self.set_username()
        results = self.smart_search(query=query, qtype=qtype,limit=limit)
        return results


    def recommendations(self, artists: Optional[List] = None, tracks: Optional[List] = None, limit=20):
        # doesnt work
        recs = self.sp.recommendations(seed_artists=artists, seed_tracks=tracks, limit=limit)
        return recs


    def get_current_track(self) -> Optional[Dict]:
        """Get information about the currently playing track"""
        try:
            # current_playback vs current_user_playing_track?
            current = self.sp.current_user_playing_track()
            if not current:
                self.logger.info("No playback session found")
                return None
            if current.get('currently_playing_type') != 'track':
                self.logger.info("Current playback is not a track")
                return None

            track_info = utils.parse_track(current['item'])
            if 'is_playing' in current:
                track_info['is_playing'] = current['is_playing']

            self.logger.info(
                f"Current track: {track_info.get('name', 'Unknown')} by {track_info.get('artist', 'Unknown')}")
            return track_info
        except Exception as e:
            self.logger.error("Error getting current track info.")
            raise

    @utils.validate
    def start_playback(self, spotify_uri=None, device=None):
        """
        Starts spotify playback of uri. If spotify_uri is omitted, resumes current playback.
        - spotify_uri: ID of resource to play, or None. Typically looks like 'spotify:track:xxxxxx' or 'spotify:album:xxxxxx'.
        """
        try:
            self.logger.info(f"Starting playback for spotify_uri: {spotify_uri} on {device}")
            if not spotify_uri:
                if self.is_track_playing():
                    self.logger.info("No track_id provided and playback already active.")
                    return
                if not self.get_current_track():
                    raise ValueError("No track_id provided and no current playback to resume.")

            if spotify_uri is not None:
                if spotify_uri.startswith('spotify:track:'):
                    uris = [spotify_uri]
                    context_uri = None
                else:
                    uris = None
                    context_uri = spotify_uri
            else:
                uris = None
                context_uri = None

            device_id = device.get('id') if device else None

            self.logger.info(f"Starting playback of on {device}: context_uri={context_uri}, uris={uris}")
            result = self.sp.start_playback(uris=uris, context_uri=context_uri, device_id=device_id)
            self.logger.info(f"Playback result: {result}")
            return result
        except Exception as e:
            self.logger.error(f"Error starting playback: {str(e)}.")
            raise

    @utils.validate
    def pause_playback(self, device=None):
        """Pauses playback."""
        playback = self.sp.current_playback()
        if playback and playback.get('is_playing'):
            self.sp.pause_playback(device.get('id') if device else None)

    @utils.validate
    def add_to_queue(self, track_id: str, device=None):
        """
        Adds track to queue.
        - track_id: ID of track to play.
        """
        self.sp.add_to_queue(track_id, device.get('id') if device else None)

    @utils.validate
    def get_queue(self, device=None):
        """Returns the current queue of tracks."""
        queue_info = self.sp.queue()
        queue_info['currently_playing'] = self.get_current_track()

        queue_info['queue'] = [utils.parse_track(track) for track in queue_info.pop('queue')]

        return queue_info

    def get_liked_songs(self):
        # todo
        results = self.sp.current_user_saved_tracks()
        for idx, item in enumerate(results['items']):
            track = item['track']
            print(idx, track['artists'][0]['name'], " – ", track['name'])

    def is_track_playing(self) -> bool:
        """Returns if a track is actively playing."""
        curr_track = self.get_current_track()
        if not curr_track:
            return False
        if curr_track.get('is_playing'):
            return True
        return False

    def get_current_user_playlists(self, limit=50) -> List[Dict]:
        """
        Get current user's playlists.
        - limit: Max number of playlists to return.
        """
        playlists = self.sp.current_user_playlists()
        if not playlists:
            raise ValueError("No playlists found.")
        return [utils.parse_playlist(playlist, self.username) for playlist in playlists['items']]
    
    @utils.ensure_username
    def get_playlist_tracks(self, playlist_id: str, limit=50) -> List[Dict]:
        """
        Get tracks from a playlist.
        - playlist_id: ID of the playlist to get tracks from.
        - limit: Max number of tracks to return.
        """
        playlist = self.sp.playlist(playlist_id)
        if not playlist:
            raise ValueError("No playlist found.")
        return utils.parse_tracks(playlist['tracks']['items'])
    
    @utils.ensure_username
    def add_tracks_to_playlist(self, playlist_id: str, track_ids: List[str], position: Optional[int] = None):
        """
        Add tracks to a playlist.
        - playlist_id: ID of the playlist to modify.
        - track_ids: List of track IDs to add.
        - position: Position to insert the tracks at (optional).
        """
        if not playlist_id:
            raise ValueError("No playlist ID provided.")
        if not track_ids:
            raise ValueError("No track IDs provided.")
        
        try:
            response = self.sp.playlist_add_items(playlist_id, track_ids, position=position)
            self.logger.info(f"Response from adding tracks: {track_ids} to playlist {playlist_id}: {response}")
        except Exception as e:
            self.logger.error(f"Error adding tracks to playlist: {str(e)}")

    @utils.ensure_username
    def remove_tracks_from_playlist(self, playlist_id: str, track_ids: List[str]):
        """
        Remove tracks from a playlist.
        - playlist_id: ID of the playlist to modify.
        - track_ids: List of track IDs to remove.
        """
        if not playlist_id:
            raise ValueError("No playlist ID provided.")
        if not track_ids:
            raise ValueError("No track IDs provided.")
        
        try:
            response = self.sp.playlist_remove_all_occurrences_of_items(playlist_id, track_ids)
            self.logger.info(f"Response from removing tracks: {track_ids} from playlist {playlist_id}: {response}")
        except Exception as e:
            self.logger.error(f"Error removing tracks from playlist: {str(e)}")

    @utils.ensure_username
    def change_playlist_details(self, playlist_id: str, name: Optional[str] = None, description: Optional[str] = None):
        """
        Change playlist details.
        - playlist_id: ID of the playlist to modify.
        - name: New name for the playlist.
        - public: Whether the playlist should be public.
        - description: New description for the playlist.
        """
        if not playlist_id:
            raise ValueError("No playlist ID provided.")
        
        try:
            response = self.sp.playlist_change_details(playlist_id, name=name, description=description)
            self.logger.info(f"Response from changing playlist details: {response}")
        except Exception as e:
            self.logger.error(f"Error changing playlist details: {str(e)}")
       
    def get_devices(self) -> dict:
        return self.sp.devices()['devices']

    def is_active_device(self):
        return any([device.get('is_active') for device in self.get_devices()])

    def _get_candidate_device(self):
        devices = self.get_devices()
        if not devices:
            raise ConnectionError("No active device. Is Spotify open?")
        for device in devices:
            if device.get('is_active'):
                return device
        self.logger.info(f"No active device, assigning {devices[0]['name']}.")
        return devices[0]

    def auth_ok(self) -> bool:
        try:
            token = self.cache_handler.get_cached_token()
            if token is None:
                self.logger.info("Auth check result: no token exists")
                return False
                
            is_expired = self.auth_manager.is_token_expired(token)
            self.logger.info(f"Auth check result: {'valid' if not is_expired else 'expired'}")
            return not is_expired  # Return True if token is NOT expired
        except Exception as e:
            self.logger.error(f"Error checking auth status: {str(e)}")
            return False  # Return False on error rather than raising

    def auth_refresh(self):
        self.auth_manager.validate_token(self.cache_handler.get_cached_token())

    def skip_track(self, n=1):
        # todo: Better error handling
        for _ in range(n):
            self.sp.next_track()

    def previous_track(self):
        self.sp.previous_track()

    def seek_to_position(self, position_ms):
        self.sp.seek_track(position_ms=position_ms)

    def set_volume(self, volume_percent):
        self.sp.volume(volume_percent)
