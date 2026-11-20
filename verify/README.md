# Screenshot verification

Capture the running app with an isolated Chromium context. The helper
blocks cross-origin browser font requests and follows the app's local font
fallbacks. It disables service workers and animations for the capture.

Install Playwright in your working Python environment and fetch its Chromium
browser. Add `--with-deps` to the browser install command when provisioning
a supported Linux environment that needs system libraries.
[Playwright installation](https://playwright.dev/python/docs/intro)

```sh
python3 -m pip install playwright
python3 -m playwright install chromium
python3 -m verify.screenshot http://127.0.0.1:8000/ evidence/home.png \
  --width 1440 --height 900 --ready 'main'
```

Start the app first, on a port assigned to your worker. Set `--ready` to
a selector that appears after the behavior you want to show. Use the URL
the Chromium process can reach. If the app and Chromium run in the same
container or host, loopback works. When the app runs in its own container
and Chromium runs on the host, bind the app to `0.0.0.0` in its container
and publish the port on the host's loopback interface. When Chromium runs
in a peer container, use the app's service name and port on the shared
network. Mount the evidence directory if the capture must persist outside
the browser container.
[Docker port publishing](https://docs.docker.com/engine/network/port-publishing/) ·
[Playwright container setup](https://playwright.dev/python/docs/docker)

Use the app origin that serves any bundled web fonts directly. The capture
documents what was visible at that viewport and point in the flow.
Run behavior assertions separately when the claim requires them.

## PR or MR evidence template

```markdown
## Verification evidence

- Commit SHA …
- Command, app route, viewport, and ready selector …
- Expected behavior …
- Observed behavior and any unverified cases …
- Screenshot or artifact link …
- Test command and result …
```
