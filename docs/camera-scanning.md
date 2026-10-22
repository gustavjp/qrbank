# QRBank mobile camera scanning

Scan Mode offers an optional camera input alongside the existing USB scanner and
manual entry. It requires a secure context (HTTPS, or localhost while developing),
a browser that grants camera access, and a working connection when the decoder is
first loaded. The camera is **never** started automatically.

## Flow and safeguards

1. Open a classroom, make it active, and enter Scan Mode.
2. Choose **Start camera scanning** and grant camera permission.
3. Scan a student QR (`STUDENT:01`), then an action or Quick Action.
4. If transaction confirmation is enabled, confirm or cancel before continuing.
5. Move a QR out of the frame before scanning that same code again, or choose
   **Stop camera** to release the camera immediately.

Camera results populate and submit the **same** `scan-form` as USB scans. No
alternate transaction endpoint is introduced. CSRF, classroom authorization,
student-selection timeouts, action validity, idempotency, and financial posting
remain entirely server-enforced. The page never sends images/video to QRBank.

The camera panel is placed outside `<main>` so the existing partial-page update
does not destroy the live video when showing a student selection or transaction
result. Scanning is ignored while a request is in progress or while an action
awaits confirmation. The reader is stopped when the user chooses Stop, hides the
tab, or leaves the page.

## QR decoder dependency

`@zxing/browser` 0.2.1 (MIT license) is loaded on first camera activation from
jsDelivr, pinned to an exact version with SHA-384 subresource integrity and
`referrerPolicy=no-referrer`. No request to the CDN occurs until the user clicks
Start. QR images are decoded locally in the browser.

If a school blocks the CDN, camera decoding will be unavailable. USB scanners
and manual QR command entry continue to work. For offline production operation,
vendor the exact verified decoder bundle and serve it from the app instead.

## Manual device checks before deployment

- On Android Chrome and iOS Safari, grant permission, scan a student and action,
  and verify the expected result and balance.
- With confirmation enabled, verify no second QR posts until confirmation.
- Keep the same QR visible for several seconds: it must not repeatedly submit.
  Remove the QR and reintroduce it to scan the same code again.
- Try an unrelated QR, a revoked/invalid code, and an expired selection.
- Check Stop, tab hide, navigation away, camera denied, insecure HTTP, and a
  blocked decoder CDN. USB scanning must remain available in every case.

CI can validate the generated controls and server route behavior but does not
simulate hardware cameras; device/browser acceptance remains a release check.
