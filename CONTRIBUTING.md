# Contributing

Thanks for helping improve HyperFlux Next. You can help with bugs,
documentation, device knowledge, packaging, application integrations, or the
bridge itself.

## Change Workflow

1. Fork the repository and create a branch for one focused change.
2. Make the change. Each top-level folder's README says what the folder holds,
   its key files, and how to check it.
3. Add or update tests for the behaviour you changed.
4. Run `./hfx generate` so generated files stay in step with your change.
5. Run `./hfx verify --fast --changed-from <commit>` while you work, where
   `<commit>` is the commit your branch started from.
6. Open a pull request that says why you made the change, how you tested it,
   and anything it might affect.

The [building](docs/developer/building.md) and
[verification](docs/developer/verification.md) guides explain the tools in more
detail.

## Find The Right Starting Point

- To report a bug, propose a feature, or improve the documentation, use the
  [issue forms](https://github.com/offalexjackson777-stack/hyperflux-next/issues/new/choose).
- To ask about a device, use the
  [device support form](https://github.com/offalexjackson777-stack/hyperflux-next/issues/new?template=device_support.yml).
- For help or early design ideas, use
  [Discussions](https://github.com/offalexjackson777-stack/hyperflux-next/discussions).
- To report a security problem privately, follow [SECURITY.md](SECURITY.md).

## Safety And Privacy

- Continuous integration has no access to real devices. Nobody will ask you to
  run tests on your hardware because you opened an issue or pull request.
- An unknown device can be described from public facts and what you observe,
  but HyperFlux will not change its lighting until support for it is added.
- Facts from other projects need a public source, its license, and a clear note
  of what is stated, what is inferred, and what is unknown.
- Do not add raw USB captures, private paths, serial numbers, machine
  identifiers, or full system logs to the repository.

## Generated Files

Some files are generated, and each one names its source at the top. Do not edit
them by hand. Change the source it names, then run `./hfx generate`.

## Review

Reviewers check that a change is focused, keeps compatibility and privacy,
has tests, and leaves generated files current.

By participating, you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
