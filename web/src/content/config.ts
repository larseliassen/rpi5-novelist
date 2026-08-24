import { defineCollection, z } from 'astro:content';

// Chapters live in the repo-root `chapters/` folder. A symlink (see README)
// exposes them to Astro at src/content/kapitler so the Pi and the site share
// exactly the same markdown.
const kapitler = defineCollection({
  type: 'content',
  schema: z.object({
    title: z.string(),
    chapter: z.number(),
    date: z.string(),
  }),
});

export const collections = { kapitler };
