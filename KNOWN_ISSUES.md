# Known issues and limits

- The execution sandbox rejects loopback socket binds for both Studio and ADB. Live app interaction, browser printing, and real model HTTP acceptance need a normal local session.
- Automated accessibility tests cover structural labels, landmark markup, alternate text and focus styling. Interactive keyboard and assistive technology review is pending.
- Recovery supports safe alternate selector selection when a selector fails before dispatch. Failed post-action assertions stop the run. Arbitrary checkpoint restoration and destructive-action detection from UI content are unavailable. Use a dedicated fixture app with reviewable actions.
- Vision analysis produces advisory output. Autonomous actions use hierarchy selectors, and manual image clicks use a fresh screenshot fingerprint. Dynamic screenshots can require a selector tap instead.
- Stop and emergency stop prevent future dispatch at checkpoints. A command already in flight can complete. Model calls and ADB operations have their own timeouts.
- Mock mode does not infer a plausible test flow from an arbitrary natural-language objective. Import a compatible configured model or edit a plan manually.
- The history shows prior runs. Detailed cross-run comparison, selector reliability statistics and app-wide navigation coverage remain incomplete.
- The live viewer currently refreshes on demand and after manual interactions that are expected to stay inside the selected app.
- Password-node redaction is best effort. Test with fixture data and avoid entering real secrets into plans or screens.
