import asyncio
import base64
import os
import logging
import sys
from enum import Enum
import json
from typing import List, Optional, Tuple
from datetime import datetime
from pathlib import Path

import mcp.types as types
from mcp.server import NotificationOptions, Server  # , stdio_server
import mcp.server.stdio
from pydantic import BaseModel, Field, AnyUrl
from spotipy import SpotifyException

from . import spotify_api
from .utils import normalize_redirect_uri


def setup_logger():
    class Logger:
        def info(self, message):
            print(f"[INFO] {message}", file=sys.stderr)

        def error(self, message):
            print(f"[ERROR] {message}", file=sys.stderr)

    return Logger()


logger = setup_logger()
# Normalize the redirect URI to meet Spotify's requirements
if spotify_api.REDIRECT_URI:
    spotify_api.REDIRECT_URI = normalize_redirect_uri(spotify_api.REDIRECT_URI)
spotify_client = spotify_api.Client(logger)

server = Server("spotify-mcp")


# options =
class ToolModel(BaseModel):
    @classmethod
    def as_tool(cls):
        return types.Tool(
            name="Spotify" + cls.__name__,
            description=cls.__doc__,
            inputSchema=cls.model_json_schema()
        )


class Playback(ToolModel):
    """Manages the current playback with the following actions:
    - get: Get information about user's current track.
    - start: Starts playing new item or resumes current playback if called with no uri.
    - pause: Pauses current playback.
    - skip: Skips current track.
    """
    action: str = Field(description="Action to perform: 'get', 'start', 'pause' or 'skip'.")
    spotify_uri: Optional[str] = Field(default=None, description="Spotify uri of item to play for 'start' action. " +
                                                                 "If omitted, resumes current playback.")
    num_skips: Optional[int] = Field(default=1, description="Number of tracks to skip for `skip` action.")


class Queue(ToolModel):
    """Manage the playback queue - get the queue or add playlists/tracks/artists/album to queue."""
    action: str = Field(description="Action to perform: 'add' or 'get'.")
    spotify_uri: Optional[str] = Field(default=None, description="Spotify resource uri to add to queue (required for add action)")


class GetInfo(ToolModel):
    """Get detailed information about a Spotify item (track, album, artist, or playlist)."""
    item_uri: str = Field(description="URI of the item to get information about. " +
                                      "If 'playlist' or 'album', returns its tracks. " +
                                      "If 'artist', returns albums and top tracks.")


class Search(ToolModel):
    """Search for tracks, albums, artists, or playlists on Spotify."""
    query: str = Field(description="query term")
    qtype: Optional[str] = Field(default="track",
                                 description="Type of items to search for (track, album, artist, playlist, " +
                                             "or comma-separated combination)")
    limit: Optional[int] = Field(default=10, description="Maximum number of items to return")


class Playlist(ToolModel):
    """Manage Spotify playlists.
    - get: Get a list of user's playlists.
    - get_tracks: Get tracks in a specific playlist.
    - add_tracks: Add tracks to a specific playlist.
    - remove_tracks: Remove tracks from a specific playlist.
    - change_details: Change details of a specific playlist.
    """
    action: str = Field(
        description="Action to perform: 'get', 'get_tracks', 'add_tracks', 'remove_tracks', 'change_details'.")
    playlist_id: Optional[str] = Field(default=None, description="ID of the playlist to manage.")
    track_ids: Optional[List[str]] = Field(default=None, description="List of track IDs to add/remove.")
    name: Optional[str] = Field(default=None, description="New name for the playlist.")
    description: Optional[str] = Field(default=None, description="New description for the playlist.")


@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    return []


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    return []


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    logger.info("Listing available tools")
    # await server.request_context.session.send_notification("are you recieving this notification?")
    tools = [
        Playback.as_tool(),
        Search.as_tool(),
        Queue.as_tool(),
        GetInfo.as_tool(),
        Playlist.as_tool(),
    ]
    logger.info(f"Available tools: {[tool.name for tool in tools]}")
    return tools


# Helper function to create error responses
def create_error_response(message):
    """Creates a standardized error response format that the LLM will recognize as an error."""
    error_response = {
        "error": True,
        "message": message
    }
    return [types.TextContent(
        type="text",
        text=json.dumps(error_response, indent=2)
    )]

def format_playback_response(spotify_uri: str) -> str:
    curr_info = spotify_client.get_info(item_uri=spotify_uri)
    logger.info(curr_info)
    uri_parts = spotify_uri.split(":")
    uri_type = uri_parts[1] if len(uri_parts) == 3 else "unknown"
    if uri_type == "track":
        title = curr_info.get("name", "Unknown Track")
        return f"▶️ Now playing: \"{title}\" by {get_artist_string(curr_info)}\nURI: {spotify_uri}"

    elif uri_type == "album":
        album = curr_info.get("name", "Unknown Album")
        total = curr_info.get("total_tracks", "N/A")
        return f"💿 Playing album: \"{album}\" by {get_artist_string(curr_info)}\nTracks: {total}\nURI: {spotify_uri}"

    elif uri_type == "playlist":
        name = curr_info.get("name", "Unknown Playlist")
        owner = curr_info.get("owner", "Unknown Owner")
        total = curr_info.get("total_tracks", "N/A")
        is_owner = curr_info.get("user_is_owner", False)
        ownership = "✅ You own this playlist" if is_owner else "👤 Owned by someone else"
        return (
            f"📜 Playing playlist: \"{name}\"\n"
            f"Owner: {owner} | Tracks: {total}\n"
            f"{ownership}\n"
            f"URI: {spotify_uri}"
        )

    elif uri_type == "artist":
        name = curr_info.get("name", "Unknown Artist")
        return f"🎤 Playing songs from artist: {name}\nURI: {spotify_uri}"

    return f"▶️ Playback started for URI: {spotify_uri} (type: {uri_type})"

@server.call_tool()
async def handle_call_tool(
        name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Handle tool execution requests."""
    logger.info(f"Tool called: {name} with arguments: {arguments}")
    assert name[:7] == "Spotify", f"Unknown tool: {name}"
    try:
        match name[7:]:
            case "Playback":
                action = arguments.get("action")
                match action:
                    case "get":
                        logger.info("Attempting to get current track")
                        curr_track = spotify_client.get_current_track()
                        if curr_track:
                            logger.info(f"Current track retrieved: {curr_track.get('name', 'Unknown')}")
                            return [types.TextContent(
                                type="text",
                                text=json.dumps(curr_track, indent=2)
                            )]
                        logger.info("No track currently playing")
                        return [types.TextContent(
                            type="text",
                            text="No track playing."
                        )]
                    case "start":
                        logger.info(f"Starting playback with arguments: {arguments}")
                        spotify_uri = arguments.get("spotify_uri")

                        # If URI is provided, validate it before attempting to play
                        if spotify_uri:
                            # Extract track ID from URI if it's a track URI
                            track_id = None
                            if spotify_uri.startswith("spotify:track:"):
                                track_id = spotify_uri.split(":")[-1]

                            # Validate the track if we have a track ID
                            if track_id and not spotify_client.is_valid_track(track_id):
                                return create_error_response(
                                    "Invalid or non-existent track URI. Please try another track.")

                        result = spotify_client.start_playback(spotify_uri=spotify_uri)
                        logger.info("Playback started successfully")

                        # Get current track details after starting playback
                        text = format_playback_response(spotify_uri)
                        return [types.TextContent(
                            type="text",
                            text=text
                        )]

                    case "pause":
                        logger.info("Attempting to pause playback")
                        spotify_client.pause_playback()
                        logger.info("Playback paused successfully")
                        return [types.TextContent(
                            type="text",
                            text="Playback paused."
                        )]
                    case "skip":
                        num_skips = int(arguments.get("num_skips", 1))
                        logger.info(f"Skipping {num_skips} tracks.")
                        spotify_client.skip_track(n=num_skips)
                        return [types.TextContent(
                            type="text",
                            text="Skipped to next track."
                        )]

            case "Search":
                try:
                    logger.info(f"Performing search with arguments: {arguments}")
                    query = arguments.get("query", "")
                    qtype = arguments.get("qtype", "track")
                    limit = arguments.get("limit", 10)

                    search_results = spotify_client.search(
                        query=query,
                        qtype=qtype,
                        limit=limit
                    )
                    logger.info(f"Search results: {search_results}")

                    formatted_results = [f"🔍 Search Results for {qtype}s:"]

                    # Map qtype to corresponding key in the response
                    result_items = search_results.get(f"{qtype}s", [])

                    if not result_items:
                        return create_error_response(f"No {qtype}s found for your query.")

                    for idx, item in enumerate(result_items, start=1):
                        if qtype == "track":
                            title = item.get("name", "Unknown Title")
                            logger.info(f"Name {item.get("name")} id {item.get('id')} item {item}")
                            uri = f"spotify:track:{item.get('id', 'N/A')}"
                            formatted_results.append(f"{idx}. \"{title}\" by {get_artist_string(item)}\n   URI: {uri}")

                        elif qtype == "artist":
                            name = item.get("name", "Unknown Artist")
                            uri = f"spotify:artist:{item.get('id', 'N/A')}"
                            formatted_results.append(f"{idx}. 👤 {name}\n   URI: {uri}")

                        elif qtype == "album":
                            title = item.get("name", "Unknown Album")
                            uri = f"spotify:album:{item.get('id', 'N/A')}"
                            formatted_results.append(f"{idx}. 💿 \"{title}\" by {get_artist_string(item)}\n   URI: {uri}")

                        elif qtype == "playlist":

                            name = item.get("name", "Unknown Playlist")

                            owner = item.get("owner", "Unknown Owner")

                            is_owner = item.get("user_is_owner", False)

                            total_tracks = item.get("total_tracks", "N/A")

                            uri = f"spotify:playlist:{item.get('id', 'N/A')}"

                            ownership_text = "✅ You own this playlist" if is_owner else "👤 Owned by someone else"

                            formatted_results.append(

                                f"{idx}. 📜 \"{name}\"\n"

                                f"   Owner: {owner} | Tracks: {total_tracks}\n"

                                f"   {ownership_text}\n"

                                f"   URI: {uri}"

                            )

                        else:
                            formatted_results.append(f"{idx}. Unsupported qtype: {qtype}")

                    return [types.TextContent(
                        type="text",
                        text="\n".join(formatted_results)
                    )]

                except Exception as e:
                    logger.error(f"Search failed: {e}")
                    return create_error_response(f"An error occurred during search: {str(e)}")

            case "Queue":
                logger.info(f"Queue operation with arguments: {arguments}")
                action = arguments.get("action")

                match action:
                    case "add":
                        spotify_uri = arguments.get("spotify_uri")
                        if not spotify_uri:
                            return create_error_response("spotify_uri is required for the 'add' action.")

                        try:
                            # Determine the type of resource (track, album, playlist, artist)
                            if "track" in spotify_uri:
                                # Handle single track
                                if not spotify_client.is_valid_track(spotify_uri):
                                    return create_error_response(
                                        "Invalid or non-existent track URI. Please try another track.")

                                # Add the track to queue
                                spotify_client.add_to_queue(spotify_uri)

                                # Get track details to confirm what was added
                                track_info = spotify_client.get_info(item_uri=spotify_uri)
                                response_data = {
                                    "status": "Track added to queue successfully",
                                    "track_details": track_info
                                }

                            elif "playlist" in spotify_uri:
                                # Handle playlist
                                if not spotify_client.is_valid_playlist(spotify_uri):
                                    return create_error_response("Invalid or non-existent playlist URI.")

                                # Get all tracks from playlist
                                playlist_tracks = spotify_client.get_playlist_tracks(spotify_uri)
                                # Add each track to queue
                                added_tracks = []
                                for track in playlist_tracks:
                                    track_uri = track.get("id")
                                    spotify_client.add_to_queue(track_uri)
                                    added_tracks.append(track)

                                playlist_info = spotify_client.get_info(item_uri=spotify_uri)
                                response_data = {
                                    "status": f"All {len(added_tracks)} tracks from playlist added to queue successfully",
                                    "playlist_details": playlist_info,
                                    "tracks_added": len(added_tracks)
                                }

                            elif "album" in spotify_uri:
                                # Handle album
                                if not spotify_client.is_valid_album(spotify_uri):
                                    return create_error_response("Invalid or non-existent album URI.")

                                # Get all tracks from album
                                album_tracks = spotify_client.get_album_tracks(spotify_uri)
                                logger.info(album_tracks)
                                # Add each track to queue
                                added_tracks = []
                                for track in album_tracks:
                                    track_uri = track.get("uri")
                                    spotify_client.add_to_queue(track_uri)
                                    added_tracks.append(track)

                                album_info = spotify_client.get_info(item_uri=spotify_uri)
                                response_data = {
                                    "status": f"All {len(added_tracks)} tracks from album added to queue successfully",
                                    "album_details": album_info,
                                    "tracks_added": len(added_tracks)
                                }

                            elif "artist" in spotify_uri:
                                # Handle artist
                                if not spotify_client.is_valid_artist(spotify_uri):
                                    return create_error_response("Invalid or non-existent artist URI.")

                                # Get top tracks from artist
                                artist_tracks = spotify_client.get_artist_top_tracks(spotify_uri)
                                logger.info(artist_tracks)
                                # Add each track to queue
                                added_tracks = []
                                for track in artist_tracks:
                                    track_uri = track.get("uri")
                                    spotify_client.add_to_queue(track_uri)
                                    added_tracks.append(track)

                                artist_info = spotify_client.get_info(item_uri=spotify_uri)
                                response_data = {
                                    "status": f"Top {len(added_tracks)} tracks from artist added to queue successfully",
                                    "artist_details": artist_info,
                                    "tracks_added": len(added_tracks)
                                }

                            else:
                                return create_error_response(
                                    "Unsupported URI type. Please provide a track, playlist, album, or artist URI.")

                            return [types.TextContent(
                                type="text",
                                text=json.dumps(response_data, indent=2)
                            )]
                        except Exception as e:
                            logger.error(e)
                            return create_error_response(f"Error adding to queue: {str(e)}")

                    case "get":
                        queue = spotify_client.get_queue()
                        return [types.TextContent(
                            type="text",
                            text=json.dumps(queue, indent=2)
                        )]

                    case _:
                        return create_error_response(
                            f"Unknown queue action: {action}. Supported actions are: add and get.")
            case "GetInfo":
                logger.info(f"Getting item info with arguments: {arguments}")
                item_info = spotify_client.get_info(
                    item_uri=arguments.get("item_uri")
                )
                return [types.TextContent(
                    type="text",
                    text=json.dumps(item_info, indent=2)
                )]

            case "Playlist":
                logger.info(f"Playlist operation with arguments: {arguments}")
                action = arguments.get("action")
                match action:
                    case "get":
                        logger.info(f"Getting current user's playlists with arguments: {arguments}")
                        playlists = spotify_client.get_current_user_playlists()
                        return [types.TextContent(
                            type="text",
                            text=json.dumps(playlists, indent=2)
                        )]
                    case "get_tracks":
                        logger.info(f"Getting tracks in playlist with arguments: {arguments}")
                        if not arguments.get("playlist_id"):
                            logger.error("playlist_id is required for get_tracks action.")
                            return create_error_response("playlist_id is required for get_tracks action.")
                        tracks = spotify_client.get_playlist_tracks(arguments.get("playlist_id"))
                        return [types.TextContent(
                            type="text",
                            text=json.dumps(tracks, indent=2)
                        )]
                    case "add_tracks":
                        logger.info(f"Adding tracks to playlist with arguments: {arguments}")
                        track_ids = arguments.get("track_ids")
                        if isinstance(track_ids, str):
                            try:
                                track_ids = json.loads(track_ids)  # Convert JSON string to Python list
                            except json.JSONDecodeError:
                                logger.error("track_ids must be a list or a valid JSON array.")
                                return create_error_response("track_ids must be a list or a valid JSON array.")

                        spotify_client.add_tracks_to_playlist(
                            playlist_id=arguments.get("playlist_id"),
                            track_ids=track_ids
                        )
                        return [types.TextContent(
                            type="text",
                            text="Tracks added to playlist."
                        )]
                    case "remove_tracks":
                        logger.info(f"Removing tracks from playlist with arguments: {arguments}")
                        track_ids = arguments.get("track_ids")
                        if isinstance(track_ids, str):
                            try:
                                track_ids = json.loads(track_ids)  # Convert JSON string to Python list
                            except json.JSONDecodeError:
                                logger.error("track_ids must be a list or a valid JSON array.")
                                return create_error_response("track_ids must be a list or a valid JSON array.")

                        spotify_client.remove_tracks_from_playlist(
                            playlist_id=arguments.get("playlist_id"),
                            track_ids=track_ids
                        )
                        return [types.TextContent(
                            type="text",
                            text="Tracks removed from playlist."
                        )]

                    case "change_details":
                        logger.info(f"Changing playlist details with arguments: {arguments}")
                        if not arguments.get("playlist_id"):
                            logger.error("playlist_id is required for change_details action.")
                            return create_error_response("playlist_id is required for change_details action.")
                        if not arguments.get("name") and not arguments.get("description"):
                            logger.error("At least one of name, description or public is required.")
                            return create_error_response(
                                "At least one of name, description, public, or collaborative is required.")

                        spotify_client.change_playlist_details(
                            playlist_id=arguments.get("playlist_id"),
                            name=arguments.get("name"),
                            description=arguments.get("description")
                        )
                        return [types.TextContent(
                            type="text",
                            text="Playlist details changed."
                        )]

                    case _:
                        return create_error_response(
                            f"Unknown playlist action: {action}. Supported actions are: get, get_tracks, add_tracks, remove_tracks, change_details.")
            case _:
                error_msg = f"Unknown tool: {name}"
                logger.error(error_msg)
                return create_error_response(error_msg)
    except SpotifyException as se:
        error_msg = f"Spotify Client error occurred: {str(se)}"
        logger.error(error_msg)
        return create_error_response(f"An error occurred with the Spotify Client: {str(se)}")
    except Exception as e:
        error_msg = f"Unexpected error occurred: {str(e)}"
        logger.error(error_msg)
        return create_error_response(error_msg)

def get_artist_string(item):
    """
    Extracts artist information from a dictionary that may contain either:
    - 'artist': a single string
    - 'artists': a list of strings

    Returns:
        A comma-separated string of artist names.
    """
    artists = item.get("artists")
    artist = item.get("artist")

    if isinstance(artists, list):
        all_artists = artists
    elif isinstance(artist, str):
        all_artists = [artist]
    else:
        all_artists = []

    return ", ".join(all_artists)


async def main():
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    except Exception as e:
        logger.error(f"Server error occurred: {str(e)}")
        raise