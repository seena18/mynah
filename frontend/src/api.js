export async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch {
      // Keep the HTTP status text when the response is not JSON.
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

export const post = (path, body) => api(path, {
  method: 'POST',
  body: JSON.stringify(body ?? {}),
});

export const put = (path, body) => api(path, {
  method: 'PUT',
  body: JSON.stringify(body),
});

export const del = (path) => api(path, { method: 'DELETE' });
