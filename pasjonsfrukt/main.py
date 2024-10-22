import os
import re
from pathlib import Path

from podme_api import PodMeDefaultAuthClient, PodMeUserCredentials, PodMeClient, PodMeEpisode
from rfeed import Item, Guid, Enclosure, Feed, Image, iTunesItem, iTunes

from .config import Config
from .utils import date_of_episode


def get_podme_client(email: str, password: str):
    return PodMeClient(
        auth_client=PodMeDefaultAuthClient(
            user_credentials=PodMeUserCredentials(
                email=email,
                password=password
            )
        )
    )


async def harvest_podcast(client: PodMeClient, config: Config, slug: str):
    if slug not in config.podcasts:
        print(f"[FAIL] The slug '{slug}' did not match any podcasts in the config file")
        return
    published_ids = await client.get_episode_ids(slug)
    if len(published_ids) == 0:
        print(f"[WARN] Could not find any published episodes for '{slug}'")
        return
    most_recent_episodes_limit = config.podcasts[slug].most_recent_episodes_limit
    if most_recent_episodes_limit is None:
        relevant_harvest_ids = published_ids
    elif most_recent_episodes_limit <= 0:
        relevant_harvest_ids = []
    else:
        relevant_harvest_ids = published_ids[-most_recent_episodes_limit:]
    harvested_ids = await harvested_episode_ids(client, config, slug)
    to_harvest = [e for e in relevant_harvest_ids if e not in harvested_ids]
    if len(to_harvest) == 0:
        print(f"[INFO] Nothing new from '{slug}', all available episodes already harvested"
              f"{f' (only looking at {most_recent_episodes_limit} most recent)' if most_recent_episodes_limit is not None else ''}")
        return
    print(
        f"[INFO] Found {len(to_harvest)} new episode{'s' if len(to_harvest) > 1 else ''} of '{slug}' ready to harvest"
        f"{f' (only looking at {most_recent_episodes_limit} most recent)' if most_recent_episodes_limit is not None else ''}"
    )
    podcast_dir = build_podcast_dir(config, slug)
    os.makedirs(podcast_dir, exist_ok=True)
    # harvest each missing episode
    for episode_id in to_harvest:
        await client.download_episode(
            str((podcast_dir / f"{episode_id}.mp3").as_posix()),  # path must be of type str
            (await client.get_episode_info(episode_id)).stream_url
        )
    await sync_slug_feed(client, config, slug)


async def harvested_episode_ids(client: PodMeClient, config: Config, slug: str):
    podcast_dir = build_podcast_dir(config, slug)
    if not podcast_dir.is_dir():
        # no directory, so clearly no harvested episodes
        return []
    episode_ids = await client.get_episode_ids(slug)
    harvested = []
    for f in podcast_dir.iterdir():
        if not f.is_file():
            continue
        m = re.match(r'(.*)\.mp3$', f.name)
        if m is not None:
            episode_id = int(m.group(1))
            if episode_id in episode_ids:
                harvested.append(episode_id)
    return harvested


def get_secret_query_parameter(config: Config):
    if config.secret is None:
        return ""  # no secret required, so don't append query parameter
    return f"?secret={config.secret}"


def sanitize_xml(content: str) -> str:
    return content.encode().decode('unicode-escape')


def build_podcast_dir(config: Config, slug: str):
    return Path(config.yield_dir) / slug


def build_podcast_feed_path(config: Config, slug: str):
    return build_podcast_dir(config, slug) / f"{config.podcasts.get(slug).feed_name}.xml"


def build_podcast_episode_file_path(config: Config, podcast_slug: str, episode_id: int):
    return build_podcast_dir(config, podcast_slug) / f"{episode_id}.mp3"


def build_feed(config: Config, episodes: list[PodMeEpisode], slug: str, title: str, description: str,
               image_url: str):
    secret_query_param = get_secret_query_parameter(config)
    items = []
    for e in episodes:
        episode_id = e.id
        episode_path = f"{slug}/{episode_id}"
        items.append(Item(
            title=e.title,
            description=e.description,
            guid=Guid(episode_id, isPermaLink=False),
            enclosure=Enclosure(
                url=f'{config.host}/{episode_path}{secret_query_param}',
                type='audio/mpeg',
                length=build_podcast_episode_file_path(config, slug, episode_id).stat().st_size
            ),
            pubDate=e.date_added,
            extensions=[
                iTunesItem(
                    author=e.author_full_name,
                    duration=e.length,
                )
            ]
        ))
    feed_link = f"{config.host}/{slug}{secret_query_param}"
    feed = Feed(
        title=title,
        link=feed_link,
        description=description,
        language="no",
        image=Image(
            url=image_url,
            title=title,
            link=feed_link
        ),
        items=sorted(items, key=lambda i: i.pubDate, reverse=True),
        extensions=[iTunes(block='Yes')]
    )
    return feed.rss()


async def sync_slug_feed(client: PodMeClient, config: Config, slug: str):
    if slug not in config.podcasts:
        print(f"[FAIL] The slug '{slug}' did not match any podcasts in the config file")
        return
    print(f"[INFO] Syncing '{slug}' feed...")
    episodes = [await client.get_episode_info(e) for e in (await harvested_episode_ids(client, config, slug))]
    podcast_info = await client.get_podcast_info(slug)
    feed = build_feed(
        config,
        episodes,
        slug,
        podcast_info.title,
        podcast_info.description,
        podcast_info.image_url
    )
    os.makedirs(build_podcast_dir(config, slug), exist_ok=True)
    with open(build_podcast_feed_path(config, slug), mode="w", encoding="utf-8") as feed_file:
        feed_file.write(feed)
    print(f"[INFO] '{slug}' feed now serving {len(episodes)} episode{'s' if len(episodes) != 1 else ''}")
