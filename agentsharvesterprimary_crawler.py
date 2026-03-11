"""
Primary Crawler Agent (H1) - Implements Obscurity Edge Protocol
Architectural Choice: Rate-limited async crawling with exponential backoff and
                     intelligent target prioritization from Firestore.
"""
import asyncio
import logging
import hashlib
import json
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse
from datetime import datetime, timedelta
import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from agents.base_agent import BaseAgent, AgentConfig
from config.firebase_config import firebase_manager

class RateLimiter:
    """Domain-based rate limiter with exponential backoff"""
    
    def __init__(self):
        self.domains: Dict[str, Dict[str, datetime]] = {}
        self.backoff_multiplier = 2.0
        self.base_delay = 1.0  # seconds
        self.max_delay = 60.0  # seconds
        
    async def wait_for_domain(self, domain: str) -> None:
        """Wait if domain has been recently accessed"""
        if domain in self.domains:
            last_access, delay = self.domains[domain]
            next_allowed = last_access + timedelta(seconds=delay)
            now = datetime.utcnow()
            
            if now < next_allowed:
                wait_time = (next_allowed - now).total_seconds()
                logging.debug(f"Rate limiting {domain}: waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
        
        # Update with base delay, will increase on errors
        self.domains[domain] = (datetime.utcnow(), self.base_delay)
    
    def increase_backoff(self, domain: str) -> None:
        """Increase backoff time for domain"""
        if domain in self.domains:
            last_access, current_delay = self.domains[domain]
            new_delay = min(current_delay * self.backoff_multiplier, self.max_delay)
            self.domains[domain] = (last_access, new_delay)
            logging.warning(f"Increased backoff for {domain} to {new_delay}s")

class PrimaryCrawler(BaseAgent):
    """Harvester Agent H1: Primary Crawler with Obscurity Edge Protocol"""
    
    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self.rate_limiter = RateLimiter()
        self.session: Optional[aiohttp.ClientSession] = None
        self.processed_repos: Set[str] = set()
        
        # Target sources
        self.target_sources = [
            self._crawl_github_trending,
            self._crawl_defi_llama,
            self._crawl_npm_popular,
            self._crawl_git_configs
        ]
    
    async def _init_session(self) -> None:
        """Initialize HTTP session with headers"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/vnd.github.v3+json',
            'Accept-Encoding': 'gzip, deflate'
        }
        
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)
    
    async def _cleanup(self) -> None:
        """Cleanup resources"""
        if self.session:
            await self.session.close()
    
    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL for rate limiting"""
        parsed = urlparse(url)
        return parsed.netloc or parsed.path
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError))
    )
    async def _fetch_with_retry(self, url: str, **kwargs) -> Optional[str]:
        """Fetch URL with retry logic and rate limiting"""
        domain = self._extract_domain(url)
        
        try:
            await self.rate_limiter.wait_for_domain(domain)
            
            if not self.session:
                await self._init_session()
            
            async with self.session.get(url, **kwargs) as response:
                if response.status == 429:  # Rate limited
                    self.rate_limiter.increase_backoff(domain)
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=429,
                        message='Rate limited'
                    )
                
                response.raise_for_status()
                return await response.text()
                
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                self.logger.warning(f"URL not found: {url}")
                return None
            elif e.status == 403:
                self.logger.warning(f"Access forbidden to: {url}")
                self.rate_limiter.increase_backoff(domain)
                raise
            else:
                self.logger.error(f"HTTP error {e.status} for {url}: {str(e)}")
                raise
        except Exception as e:
            self.logger.error(f"Error fetching {url}: {str(e)}")
            raise
    
    async def _crawl_github_trending(self) -> List[Dict[str, str]]:
        """Crawl GitHub trending repositories"""
        repos = []
        try:
            # Note: GitHub doesn't have official trending API
            # Using search API as alternative
            url = "https://api.github.com/search/repositories"
            params = {
                'q': 'stars:>1000',
                'sort': 'stars',
                'order': 'desc',
                'per_page': 50
            }
            
            data = await self._fetch_with_retry(url, params=params)
            if data:
                result = json.loads(data)
                for item in result.get('items', []):
                    repo_info = {
                        'url': item['html_url'],
                        'name': item['full_name'],
                        'description': item.get('description', ''),
                        'stars': item['stargazers_count'],
                        'language': item.get('language', ''),
                        'source': 'github_trending'
                    }
                    repos.append(repo_info)
            
        except Exception as e:
            self.logger.error(f"GitHub trending crawl failed: {str(e)}")
        
        return repos
    
    async def _crawl_defi_llama(self) -> List[Dict[str, str]]:
        """Crawl DeFi Llama for protocol repositories"""
        repos = []
        try:
            url = "https://api.llama.fi/protocols"
            data = await self._fetch_with_retry(url)
            
            if data:
                protocols = json.loads(data)
                for protocol in protocols[:50]:  # Limit to top 50
                    if 'url' in protocol and 'github' in protocol['url']:
                        repo_info