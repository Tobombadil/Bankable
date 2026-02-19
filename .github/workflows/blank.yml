export default {
  async fetch(request) {
    const url = new URL(request.url);
    // Rewrite origin to Lovable's published app
    url.hostname = "bankablehq.lovable.app";

    const newRequest = new Request(url.toString(), {
      method: request.method,
      headers: request.headers,
      body: request.body,
      redirect: "follow",
    });

    return fetch(newRequest);
  }
};
