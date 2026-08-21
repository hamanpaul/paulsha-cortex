# #718 runtime-instance-authority

- Persisted the exact Manager-issued template instance into durable job rows and
  switched harvest/gate spool consumers to that validated authority instead of
  re-deriving slots from internal job ids, session names, or log-path siblings.
