-- Turns README.md into git-stack's man page (see build.py). Puts a NAME and
-- SYNOPSIS before the README's introduction, which becomes the DESCRIPTION,
-- drops the sections about installing and developing git-stack, makes the
-- README's sections the page's sections and its commands their subsections,
-- and turns links within the README into plain text.

local DROPPED = {["Install"] = true, ["Development"] = true}

local OPENING = [[
# NAME

git-stack - stacked branches that survive squash merges

# SYNOPSIS

**git stack** [**-C** *path*] *command* [*args*]

# DESCRIPTION
]]

function Pandoc(doc)
  local blocks = pandoc.read(OPENING, "markdown").blocks
  local dropping = false
  for _, block in ipairs(doc.blocks) do
    if block.t == "Header" and block.level == 1 then
      -- The README's title; the page's header names it instead.
    elseif block.t == "Header" and block.level == 2 then
      dropping = DROPPED[pandoc.utils.stringify(block)] or false
      if not dropping then
        blocks:insert(pandoc.Header(1, pandoc.text.upper(pandoc.utils.stringify(block))))
      end
    elseif not dropping then
      if block.t == "Header" then
        block.level = block.level - 1
      end
      blocks:insert(block)
    end
  end
  doc.blocks = blocks
  doc.meta.title = "GIT-STACK"
  doc.meta.section = "1"
  doc.meta.header = "Git Stack Manual"
  doc.meta.footer = "git-stack"
  return doc
end

function Link(link)
  if link.target:sub(1, 1) == "#" then
    return link.content
  end
end
