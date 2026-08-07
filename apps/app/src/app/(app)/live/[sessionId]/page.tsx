"use client";

/**
 * Screen-pop deep link — /live/<sessionId>.
 *
 * Dialers/CRMs open this URL to pop the live monitor view for a call
 * already in progress. This route holds no UI of its own: it's a
 * thin bootstrap that hands off to /coaching's existing monitor
 * experience via the ``sessionId`` query param, which auto-mints a
 * monitor ticket and joins on mount (see CoachingPage in
 * ``(app)/coaching/page.tsx``). Reuses that page's components rather
 * than duplicating the transcript/suggestions UI here.
 */

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

export default function LiveDeepLinkPage() {
    const router = useRouter();
    const params = useParams<{ sessionId: string }>();
    const sessionId = params?.sessionId;

    useEffect(() => {
        if (!sessionId) return;
        router.replace(`/coaching?sessionId=${encodeURIComponent(sessionId)}`);
    }, [sessionId, router]);

    return (
        <div className="flex min-h-[40vh] items-center justify-center text-sm text-text-muted">
            Joining live session…
        </div>
    );
}
