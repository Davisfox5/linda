"use client";

/**
 * Speaker-labeled, autoscrolling transcript list.
 *
 * Extracted from the /coaching page's ``TranscriptPanel`` so the
 * public embed widget (``/embed/live/[sessionId]``) can reuse the
 * exact same partial-replaced-by-final rendering and autoscroll/
 * jump-to-live behavior instead of duplicating it. Callers own their
 * own outer chrome (border, header, height) — this component is just
 * the scroll region + line list.
 */

import { useEffect, useRef, useState } from "react";
import { speakerLabel, type TranscriptLine } from "@/lib/live-coaching";

export interface TranscriptViewProps {
    transcript: TranscriptLine[];
    /** Tailwind height utility for the scroll region. Defaults to the
     *  /coaching page's fixed panel height; the embed widget passes
     *  ``h-full`` to fill its iframe. */
    heightClassName?: string;
    emptyMessage?: string;
    jumpToLiveLabel?: string;
}

export function TranscriptView({
    transcript,
    heightClassName = "h-[60vh]",
    emptyMessage = "Waiting for the first turn…",
    jumpToLiveLabel = "Jump to live",
}: TranscriptViewProps) {
    const scrollRef = useRef<HTMLDivElement | null>(null);
    const [autoscroll, setAutoscroll] = useState(true);
    const [jumped, setJumped] = useState(false);

    useEffect(() => {
        const el = scrollRef.current;
        if (!el) return;
        if (!autoscroll) return;
        // Stick to the bottom on every new line. The "jumped" pill is
        // surfaced only when the user manually scrolls up — that flips
        // ``autoscroll`` to false in the scroll handler below.
        el.scrollTop = el.scrollHeight;
    }, [transcript.length, autoscroll]);

    return (
        <div className="relative">
            {jumped && !autoscroll ? (
                <button
                    type="button"
                    onClick={() => {
                        setAutoscroll(true);
                        setJumped(false);
                    }}
                    className="absolute right-3 top-2 z-10 rounded-full border border-primary/30 bg-bg-card/95 px-3 py-0.5 text-xs text-primary shadow-sm hover:bg-primary/10"
                >
                    {jumpToLiveLabel}
                </button>
            ) : null}
            <div
                ref={scrollRef}
                onScroll={(e) => {
                    const el = e.currentTarget;
                    const atBottom =
                        el.scrollHeight - el.scrollTop - el.clientHeight < 40;
                    setAutoscroll(atBottom);
                    setJumped(!atBottom);
                }}
                className={`${heightClassName} overflow-y-auto px-4 py-3 space-y-2 text-sm`}
            >
                {transcript.length === 0 ? (
                    <div className="text-text-muted">{emptyMessage}</div>
                ) : (
                    transcript.map((line) => (
                        <div
                            key={line.id}
                            className={`leading-relaxed ${
                                line.isFinal ? "text-text" : "text-text-muted italic"
                            }`}
                        >
                            <span className="text-xs font-semibold uppercase tracking-wide text-text-subtle mr-2">
                                {speakerLabel(line.speaker)}
                            </span>
                            {line.text}
                        </div>
                    ))
                )}
            </div>
        </div>
    );
}
