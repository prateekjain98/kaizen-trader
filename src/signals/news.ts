/**
 * CryptoPanic news signal fetcher.
 *
 * Scores each token's news sentiment by:
 *  - Headline keyword matching (positive / negative lexicon)
 *  - Vote velocity ratio (bullish votes / bearish votes over 24h)
 *  - Mention velocity vs 7-day rolling baseline (spike detection)
 *
 * Returns a sentiment score [-1, +1] per symbol, clamped.
 */

import { env } from '../config.js';
import { log } from '../storage/database.js';

// ─── Types ────────────────────────────────────────────────────────────────────

interface CPPost {
  id: number;
  title: string;
  published_at: string;
  currencies: Array<{ code: string }>;
  votes: {
    positive: number;
    negative: number;
    important: number;
    liked: number;
    disliked: number;
    lol: number;
    toxic: number;
    saved: number;
    comments: number;
  };
}

interface CPResponse {
  results: CPPost[];
}

export interface NewsSentiment {
  symbol: string;
  score: number;       // -1 to +1
  mentionCount: number;
  topHeadlines: string[];
  velocityRatio: number; // current / 7d baseline
  sampledAt: number;
}

// ─── Lexicons ─────────────────────────────────────────────────────────────────

const BULLISH_TERMS = [
  'partnership', 'integration', 'launch', 'listing', 'upgrade', 'milestone',
  'bullish', 'adoption', 'record', 'surge', 'rally', 'growth', 'wins',
  'mainnet', 'release', 'staking', 'airdrop',
];

const BEARISH_TERMS = [
  'hack', 'exploit', 'breach', 'fraud', 'scam', 'rug', 'ban', 'banned',
  'regulation', 'lawsuit', 'sec', 'crash', 'bearish', 'suspend', 'delisted',
  'delisting', 'investigation', 'bankruptcy', 'insolvent',
];

function scoreHeadline(title: string): number {
  const lower = title.toLowerCase();
  let score = 0;
  for (const term of BULLISH_TERMS) if (lower.includes(term)) score += 0.15;
  for (const term of BEARISH_TERMS) if (lower.includes(term)) score -= 0.25;
  return Math.max(-1, Math.min(1, score));
}

function scoreVotes(votes: CPPost['votes']): number {
  const pos = votes.positive + votes.liked + votes.important;
  const neg = votes.negative + votes.disliked + votes.toxic;
  const total = pos + neg;
  if (total < 3) return 0;
  return Math.max(-1, Math.min(1, (pos - neg) / total));
}

// ─── Rolling baseline tracker ─────────────────────────────────────────────────

const mentionHistory = new Map<string, number[]>(); // symbol → daily mention counts (last 7)

function updateBaseline(symbol: string, count: number): number {
  if (!mentionHistory.has(symbol)) mentionHistory.set(symbol, []);
  const hist = mentionHistory.get(symbol)!;
  hist.push(count);
  if (hist.length > 7) hist.shift();
  const avg = hist.reduce((a, b) => a + b, 0) / hist.length;
  return avg > 0 ? count / avg : 1;
}

// ─── Fetch ────────────────────────────────────────────────────────────────────

let lastFetchAt = 0;
let cached: NewsSentiment[] = [];
const CACHE_TTL_MS = 300_000; // 5 min

export async function fetchNewsSentiment(symbols: string[]): Promise<NewsSentiment[]> {
  if (!env.cryptoPanicToken) return [];
  if (Date.now() - lastFetchAt < CACHE_TTL_MS) return cached;

  const url = `https://cryptopanic.com/api/free/v1/posts/?auth_token=${env.cryptoPanicToken}&public=true&kind=news`;

  let data: CPResponse;
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(8_000) });
    if (!res.ok) {
      log('warn', `CryptoPanic fetch failed: ${res.status}`);
      return cached;
    }
    data = await res.json() as CPResponse;
  } catch (err) {
    log('warn', `CryptoPanic network error: ${String(err)}`);
    return cached;
  }

  // Group posts by symbol
  const bySymbol = new Map<string, CPPost[]>();
  for (const post of data.results) {
    for (const currency of post.currencies) {
      const sym = currency.code.toUpperCase();
      if (!symbols.includes(sym)) continue;
      if (!bySymbol.has(sym)) bySymbol.set(sym, []);
      bySymbol.get(sym)!.push(post);
    }
  }

  const sentiments: NewsSentiment[] = [];

  for (const symbol of symbols) {
    const posts = bySymbol.get(symbol) ?? [];
    if (posts.length === 0) continue;

    const headlineScores = posts.map(p => scoreHeadline(p.title));
    const voteScores = posts.map(p => scoreVotes(p.votes));
    const avgScore = (headlineScores.reduce((a, b) => a + b, 0) + voteScores.reduce((a, b) => a + b, 0)) / (headlineScores.length + voteScores.length);
    const velocityRatio = updateBaseline(symbol, posts.length);

    sentiments.push({
      symbol,
      score: Math.max(-1, Math.min(1, avgScore)),
      mentionCount: posts.length,
      topHeadlines: posts.slice(0, 3).map(p => p.title),
      velocityRatio,
      sampledAt: Date.now(),
    });
  }

  lastFetchAt = Date.now();
  cached = sentiments;
  return sentiments;
}
