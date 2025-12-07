'use client';

import React, { useState, useEffect, useRef } from 'react';
import { get } from '@/lib/apiClient';

/* ---------- Types based on your FastAPI responses ---------- */

interface LatestRecord {
  device: string;
  lat?: number | null;
  lon?: number | null;
  timestamp?: string | null;
  status?: string;
  audio_url?: string | null;
  audio_ts?: string | null;
}

interface ResolveTokenResponse {
  ok: boolean;
  device: string;
  latest?: LatestRecord | null;
}

/* ---------- Component ---------- */

export default function AutoHomePage() {
  const [token, setToken] = useState<string | null>(null);
  const [device, setDevice] = useState<string | null>(null);
  const [latest, setLatest] = useState<LatestRecord | null>(null);
  const [status, setStatus] = useState<
    'idle' | 'resolving' | 'polling' | 'error' | 'missing'
  >('idle');
  const [error, setError] = useState<Error | null>(null);

  const pollRef = useRef<NodeJS.Timeout | null>(null);
  const POLL_MS = 3000;

  /* -------------------- On Mount: Read URL Params -------------------- */
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const t = params.get('token');
    const d = params.get('device');

    if (t) {
      setToken(t);
      setStatus('resolving');
      resolveTokenAndStart(t);
    } else if (d) {
      setDevice(d);
      setStatus('polling');
      startPolling(d);
    } else {
      setStatus('missing');
    }

    return () => stopPolling();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* -------------------- Resolve Token -------------------- */
  async function resolveTokenAndStart(t: string) {
    try {
      const res = (await get(
        `resolve-token?token=${encodeURIComponent(t)}`
      )) as ResolveTokenResponse;

      setDevice(res.device);
      setLatest(res.latest || null);
      setStatus('polling');
      startPolling(res.device);
    } catch (err: any) {
      console.error('resolve-token failed', err);
      setError(err);
      setStatus(err.status === 404 ? 'missing' : 'error');
    }
  }

  /* -------------------- Polling -------------------- */
  function startPolling(dev: string) {
    stopPolling();
    fetchLocation(dev);
    pollRef.current = setInterval(() => fetchLocation(dev), POLL_MS);
  }

  function stopPolling() {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  async function fetchLocation(dev: string) {
    try {
      const rec = (await get(
        `location?device=${encodeURIComponent(dev)}`
      )) as LatestRecord;
      setLatest(rec);
      setError(null);
    } catch (err: any) {
      console.warn('fetchLocation failed', err);
      setError(err);
    }
  }

  /* -------------------- Build audio URL -------------------- */
  function buildAudioUrl(audioPath?: string | null): string | null {
    if (!audioPath) return null;
    if (audioPath.startsWith('http')) return audioPath;

    const apiBase = process.env.NEXT_PUBLIC_API_URL ?? '';
    return audioPath.startsWith('/')
      ? `${apiBase}${audioPath}`
      : `${apiBase}/static/audio/${audioPath}`;
  }

  /* -------------------- UI States -------------------- */

  if (status === 'missing') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 p-6">
        <div className="max-w-xl text-center bg-white p-6 rounded shadow">
          <h1 className="text-xl font-semibold mb-2">sheSafe — Live</h1>
          <p className="text-sm text-gray-600 mb-4">
            Open this page with a token or device in the URL. Example:
          </p>
          <div className="font-mono text-sm bg-gray-100 p-2 rounded">
            <div>/?token=abc123</div>
            <div className="mt-1">/?device=device123</div>
          </div>
          <p className="text-xs text-gray-400 mt-4">
            Token resolves automatically and the page will show location & audio.
          </p>
        </div>
      </div>
    );
  }

  if (status === 'resolving') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 p-6">
        <div className="bg-white p-6 rounded shadow text-center">
          <div className="text-sm text-gray-600">Resolving token...</div>
          <div className="mt-3 font-mono text-lg">{token}</div>
        </div>
      </div>
    );
  }

  /* -------------------- MAIN LIVE UI -------------------- */

  return (
    <div className="min-h-screen bg-gray-50 p-6">
      <div className="max-w-3xl mx-auto space-y-6">
        <header className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold">sheSafe — Live</h1>
          <div className="text-sm text-gray-500">
            {device ? `Device: ${device}` : 'Resolving...'}
          </div>
        </header>

        <section className="bg-white p-4 rounded shadow">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* ---- Location ---- */}
            <div>
              <div className="text-sm text-gray-500">Location</div>
              <div className="mt-2">
                {latest?.lat && latest?.lon ? (
                  <>
                    <div className="font-mono">
                      {latest.lat}, {latest.lon}
                    </div>
                    <a
                      className="text-sm underline mt-2 inline-block"
                      target="_blank"
                      rel="noreferrer"
                      href={`https://www.google.com/maps/search/?api=1&query=${latest.lat},${latest.lon}`}
                    >
                      Open in Google Maps
                    </a>
                    <div className="text-xs text-gray-400 mt-1">
                      Last seen: {latest.timestamp ?? '—'}
                    </div>
                  </>
                ) : (
                  <div className="text-sm text-gray-500">
                    Location not available yet
                  </div>
                )}
              </div>
            </div>

            {/* ---- Audio ---- */}
            <div>
              <div className="text-sm text-gray-500">Recording</div>
              <div className="mt-2">
                {latest?.audio_url ? (
                  <div>
                    <audio controls src={buildAudioUrl(latest.audio_url)} />
                    <div className="text-xs text-gray-400 mt-1">
                      Audio ts: {latest.audio_ts ?? '—'}
                    </div>
                  </div>
                ) : (
                  <div className="text-sm text-gray-500">
                    No recording yet
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Error */}
          {error && (
            <div className="mt-3 text-red-600 text-sm">
              Error: {error.message}
            </div>
          )}

          <div className="mt-4 flex items-center space-x-3">
            <button
              onClick={() => device && fetchLocation(device)}
              className="px-3 py-1 rounded border"
            >
              Refresh now
            </button>
            <button
              onClick={() => {
                stopPolling();
                setDevice(null);
                setLatest(null);
                setStatus('missing');

                try {
                  const url = new URL(window.location.href);
                  url.searchParams.delete('token');
                  url.searchParams.delete('device');
                  window.history.replaceState({}, '', url.toString());
                } catch {}
              }}
              className="px-3 py-1 rounded border text-sm"
            >
              Stop
            </button>
          </div>
        </section>

        <footer className="text-xs text-gray-500 text-center">
          Polling every {POLL_MS / 1000}s • Backend:{' '}
          {process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'}
        </footer>
      </div>
    </div>
  );
}
