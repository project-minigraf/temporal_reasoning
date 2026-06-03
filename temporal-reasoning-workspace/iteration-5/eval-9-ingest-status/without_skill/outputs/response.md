# Eval 9 — Without Skill
## Tool calls
(none)
## Response to user
I don't have a way to check the status of any ongoing indexing process from here. I don't have visibility into whether an indexing job is running, how far along it is, or when it completed.

To find out if indexing is done, you could:
- Check the logs or output of whatever tool or script kicked off the indexing
- Look for a status file or database entry that the indexer writes when it finishes
- Run the indexing command again — many tools are idempotent and will either skip already-indexed content or tell you it's up to date

Once indexing is confirmed complete, I'm happy to help you write a query to find which modules depend on the auth package. Just let me know what query interface or tool you're using (e.g. a graph database, a code analysis tool, a custom query language) and I can assist from there.
