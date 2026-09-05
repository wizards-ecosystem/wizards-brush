# Third-party notices

The Wizard's Brush source is Apache-2.0. This file records material that is
bundled into the generated frontend under another permissive license. The
complete copyright notices and license texts shipped with that browser bundle
are available at `/third-party-notices.txt` and in
`frontend/public/third-party-notices.txt` in the source tree.

## Browser JavaScript and generated CSS

The production dependency closure is recorded in `frontend/package-lock.json`.
The generated browser bundle incorporates MIT-licensed code from:

- React, React DOM, and Scheduler, copyright Meta Platforms, Inc. and affiliates.
- React Router and React Router DOM, copyright React Training LLC, Remix
  Software Inc., and Shopify Inc.
- Zustand, copyright Paul Henschel.
- cookie, copyright Roman Shtylman and Douglas Christopher Wilson.
- set-cookie-parser, copyright Nathan Friedly.
- Tailwind CSS and its PostCSS integration, copyright Tailwind Labs, Inc. The
  build compiler contributes its reset and generated utility CSS to the bundle.
- Vite, copyright VoidZero Inc. and Vite contributors, and Rolldown, copyright
  VoidZero Inc. and contributors. The production JavaScript contains the Vite
  module-preload polyfill and generated bundler bootstrap code.

The frontend optimizer can remove modules unused by a particular build, and
most build tools do not contribute their implementation to the result. The
served notice conservatively retains the runtime closure and build-tool code
emitted into the browser assets so a release never loses an applicable notice
because bundling details changed.

## Instrument Sans

Copyright 2022 The Instrument Sans Project Authors
(<https://github.com/Instrument/instrument-sans>).

The font software is licensed under the SIL Open Font License, Version 1.1.

## Newsreader

Copyright 2020 The Newsreader Project Authors
(<https://github.com/productiontype/Newsreader>).

The font software is licensed under the SIL Open Font License, Version 1.1.

The full OFL-1.1 text and both copyright notices are included in the generated
frontend as `/third-party-notices.txt`. Upstream package copies live in
`@fontsource-variable/instrument-sans` and `@fontsource-variable/newsreader`.

## Other dependencies

Python packages are downloaded into each user's project-local environment and
are not redistributed in the source or standalone archive. They retain their
own licenses and are inventoried in `uv.lock`. Some installed CUDA runtime
packages and the NVIDIA driver are proprietary and remain under NVIDIA terms.
Model and adapter terms are catalogued separately in
[docs/model-licenses.md](docs/model-licenses.md).
