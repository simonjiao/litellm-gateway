import { toast } from 'svelte-sonner';
import { get } from 'svelte/store';
import { chatId } from '$lib/stores';

type Translate = (key: string) => string;
const pending = new Set<string>();

export function isAttachmentLink(href: string): boolean {
	if (href.startsWith('sandbox:')) return true;
	try {
		const url = new URL(href, window.location.origin);
		return url.origin === window.location.origin && (
			/^\/api\/agent\/(?:artifacts\/[^/]+|files\/[^/]+|candidates)\/download$/.test(url.pathname) ||
			/^\/api\/v1\/files\/[^/]+\/content(?:\/.*)?$/.test(url.pathname)
		);
	} catch {
		return false;
	}
}

function downloadUrl(href: string): URL {
	if (href.startsWith('sandbox:')) {
		const match = href.match(/^sandbox:\/workspace\/outputs\/([A-Za-z0-9_-]{1,128})\/(.+)$/);
		if (!match || !get(chatId)) throw new Error('File is unavailable.');
		const url = new URL('/api/agent/candidates/download', window.location.origin);
		url.search = new URLSearchParams({ chat_id: get(chatId), message_id: match[1], path: href }).toString();
		return url;
	}
	if (!isAttachmentLink(href)) throw new Error('File is unavailable.');
	return new URL(href, window.location.origin);
}

function errorMessage(status: number): string {
	if (status === 409) return 'File is still being prepared. Please try again shortly.';
	if (status === 401) return 'Please sign in again to download this file.';
	if ([403, 404, 410].includes(status)) return 'File is unavailable or you do not have access.';
	return 'Download failed. Please try again.';
}

export async function downloadAttachment(href: string, t: Translate): Promise<void> {
	if (pending.has(href)) return;
	pending.add(href);
	const notice = toast.loading(t('Preparing download…'));
	try {
		const url = downloadUrl(href);
		url.searchParams.set('check', 'true');
		const response = await fetch(url, {
			credentials: 'same-origin', cache: 'no-store',
			headers: { Authorization: `Bearer ${localStorage.token}` },
			signal: AbortSignal.timeout(30000)
		});
		if (!response.ok) throw new Error(errorMessage(response.status));
		const target = downloadUrl((await response.json()).url);
		// A browser download streams to disk without buffering a Blob in the page.
		// Keep any error between the check and GET inside an invisible same-origin frame.
		const frame = document.createElement('iframe');
		frame.hidden = true;
		frame.title = t('Download');
		frame.onload = () => {
			if (frame.contentDocument?.URL === 'about:blank') return;
			try {
				toast.error(t('Download failed. Please try again.'));
			} finally { frame.remove(); }
		};
		frame.src = target.href;
		document.body.appendChild(frame);
		// Successful attachments are handed off to the browser download manager.
		window.setTimeout(() => frame.remove(), 3600000);
	} catch (error) {
		toast.error(t(error instanceof Error && !['TimeoutError', 'TypeError'].includes(error.name)
			? error.message : 'Download failed. Please try again.'));
	} finally {
		toast.dismiss(notice);
		pending.delete(href);
	}
}

export async function downloadFile(id: string | null, loading: boolean, t: Translate): Promise<void> {
	if (loading || !id) {
		toast.info(t('File is still being prepared. Please try again shortly.'));
		return;
	}
	const kind = /^artifact_[a-f0-9]{32}$/.test(id) ? 'artifacts' : 'files';
	await downloadAttachment(`/api/agent/${kind}/${encodeURIComponent(id)}/download`, t);
}
