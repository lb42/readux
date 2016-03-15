'''Methods to generate annotated TEI for export.'''

from datetime import datetime
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils.text import slugify
from eulxml.xmlmap import load_xmlobject_from_string, teimap
import logging
from lxml import etree
import mistune

from readux import __version__
from readux.books import tei, markdown_tei
from readux.utils import absolutize_url


logger = logging.getLogger(__name__)


def annotated_tei(teivol, annotations):
    '''Takes a TEI :class:`~readux.books.tei.Facsimile` document and an
    :class:`~readux.annotation.models.Annotation` queryset,
    and generates an annotated TEI Facsimile document.  Tei header is
    updated to reflect annotated edition, with a responsibility based
    on the users associated with the annotations passed in.  Annotation
    references are added to the facsimile data in the form of start and
    end anchors for text annotations and new zones for image annotations.
    Annotation content is added to the body of the TEI, and annotation
    tags, if any, are added as an interpGrp.

    :param teivol: tei document to be annotated; expects
        :class:`~readux.books.tei.Facsimile`
    :param annotations: :class:`~readux.annotations.models.Annotation`
        queryset to be added into the TEI for export
    :returns: :class:`~readux.books.tei.AnnotatedFacsimile` with annotation
        information
    '''
    # iterate throuth the annotations associated with this volume
    # and insert them into the tei based on the content they reference

    # could do some sanity-checking: compare annotation total vs
    # number actually added as we go page-by-page?

    tags = set()
    # make sure tei xml is using the xmlobject we need to add
    # annotation data and tags
    if not isinstance(teivol, tei.AnnotatedFacsimile):
        teivol = tei.AnnotatedFacsimile(teivol.node)

    # update title to reflect annotated version being exported
    teivol.main_title = teivol.title
    teivol.subtitle = ", an annotated digital edition"
    del teivol.title  # delete old, unqualified title

    # update responsibility statement
    teivol.responsibility = 'annotated by'
    # get a distinct list of all annotation authors
    # NOTE: this will add users even if the annotations don't get
    # successfully added to the output
    user_ids = annotations.only('user').order_by('user')\
                       .values_list('user', flat=True).distinct()
    users = get_user_model().objects.filter(id__in=user_ids)
    for user in users:
        teivol.responsible_names.append(tei.Name(id=user.username,
                                                 value=user.get_full_name()))

    # publication statement - main info should already be set
    # update to reflect annotated tei and ensure date is current
    teivol.create_pubstmt()  # make sure publicationStmt exists
    teivol.pubstmt.desc = 'Annotated TEI generated by Readux version %s' % __version__
    export_date = datetime.now()
    teivol.pubstmt.date = export_date
    teivol.pubstmt.date_normal = export_date

    for page in teivol.page_list:
        # use page.href to find annotations for this page
        # if for some reason href is not set, skip this page
        if not page.href:
            continue

        # page.href should either be local readux uri OR ARK uri;
        # local uri is stored as annotation uri, but ark is in extra data
        page_annotations = annotations.filter(Q(uri=page.href)|Q(extra_data__contains=page.href))
        if page_annotations.exists():
            for note in page_annotations:
                # possible to get extra matches for page url in related pages,
                # so skip any notes where ark doesn't match page url
                if note.extra_data.get('ark', '') != page.href:
                    continue

                insert_note(teivol, page, note)
                # collect a list of unique tags as we work through the notes
                if 'tags' in note.info():
                    tags |= set(t.strip() for t in note.info()['tags'])

    # tags are included in the back matter as an interpGrp
    if tags:
        # create back matter interpgrp for annotation tags
        teivol.create_tags()
        for tag in tags:
            # NOTE: our tag implementation currently does not allow spaces,
            # but using slugify to generate ids to avoid any issues with spaces
            # and variation in capitalization or punctuation
            teivol.tags.interp.append(tei.Interp(id=slugify(tag), value=tag))

    return teivol


def annotation_to_tei(annotation, teivol):
    '''Generate a tei note from an annotation.  Sets annotation id,
    slugified tags as ana attribute, username as resp attribute, and
    annotation content is converted from markdown to TEI.

    :param annotation: :class:`~readux.annotations.models.Annotation`
    :param teivol: :class:`~readux.books.tei.AnnotatedFacsimile` tei
        document, for converting related page ARK uris into TEI ids
    :returns: :class:`readux.books.tei.Note`
    '''
    # NOTE: annotation created/edited dates are not included here
    # because they were determined not to be relevant for our purposes

    # sample note provided by Alice
    # <note resp="JPK" xml:id="oshnp50n1" n="1"><p>This is an example note.</p></note>

    # convert markdown-formatted text content to tei
    note_content = markdown_tei.convert(annotation.text)
    # markdown results could be a list of paragraphs, and not a proper
    # xml tree; also, pags do not include namespace
    # wrap in a note element and set the default namespace as tei
    teinote = load_xmlobject_from_string('<note xmlns="%s">%s</note>' % \
        (teimap.TEI_NAMESPACE, note_content),
        tei.Note)

    # what id do we want? annotation uuid? url?
    teinote.id = 'annotation-%s' % annotation.id  # can't start with numeric
    teinote.href = absolutize_url(annotation.get_absolute_url())
    teinote.type = 'annotation'

    # if an annotation includes tags, reference them by slugified id in @ana
    if 'tags' in annotation.info() and annotation.info()['tags']:
        tags = ' '.join(set('#%s' % slugify(t.strip())
                            for t in annotation.info()['tags']))
        teinote.ana = tags

    # if the annotation has an associated user, mark the author
    # as responsible for the note
    if annotation.user:
        teinote.resp = annotation.user.username

    # include full markdown of the annotation, as a backup for losing
    # content converting from markdown to tei, and for easy display
    teinote.markdown = annotation.text

    # if annotation contains related pages, generate a link group
    if annotation.related_pages:
        for rel_page in annotation.related_pages:
            page_ref = tei.Ref(text=rel_page, type='related page')
            # find tei page identifier from the page ark
            target = teivol.page_id_by_xlink(rel_page)
            if target is not None:
                page_ref.target = '#%s' % target
            teinote.related_pages.append(page_ref)

    return teinote

def html_xpath_to_tei(xpath):
    '''Convert xpaths generated on the readux site to the
    equivalent xpaths for the corresponding TEI content,
    so that annotations created against the HTML can be matched to
    the corresponding TEI.'''
    # NOTE: span could match either line in abbyy ocr or word in mets/alto
    return xpath.replace('div', 'tei:zone') \
                .replace('span', 'node()[local-name()="line" or local-name()="w"]') \
                .replace('@id', '@xml:id')


def insert_note(teivol, teipage, annotation):
    '''Insert an annotation and highlight reference into a tei document
    and tei facsimile page.

    :param teivol: :class:`~readux.books.tei.AnnotatedFacsimile` tei
        document where the tei note should be added
    :param teipage: :class:`~readux.books.tei.Zone` page zone where
        annotation highlight references should be added
    :param annotation: :class:`~readux.annotations.models.Annotation`
        to add the document
    '''

    info = annotation.info()
    # convert html xpaths to tei
    if 'ranges' in info and info['ranges']:
        # NOTE: assuming a single range selection for now
        # the annotator model supports multiple, but UI does not currently
        # support it.
        selection_range = info['ranges'][0]
        # convert html xpaths from readux website to equivalent tei xpaths
        # for selection within the facsimile document
        # either of start or end xpaths could be empty; if so, assume
        # starting at the beginning of the page or end at the end
        start_xpath = html_xpath_to_tei(selection_range['start']) or '//tei:zone[1]'
        end_xpath = html_xpath_to_tei(selection_range['end']) or '//tei:zone[last()]'
        # insert references using start and end xpaths & offsets
        start = teipage.node.xpath(start_xpath, namespaces=tei.Zone.ROOT_NAMESPACES)
        end = teipage.node.xpath(end_xpath, namespaces=tei.Zone.ROOT_NAMESPACES)
        if not start or not end:
            logger.warn('Could not find start or end xpath for annotation %s' % annotation.id)
            return
        else:
            # xpath returns a list of matches; we only want the first one
            start = start[0]
            end = end[0]

        start_anchor = tei.Anchor(type='text-annotation-highlight-start',
            id='highlight-start-%s' % annotation.id,
            next='highlight-end-%s' % annotation.id)
        end_anchor = tei.Anchor(type='text-annotation-highlight-end',
            id='highlight-end-%s' % annotation.id)

        # insert the end *first* in case start and end are in the
        # same element; otherwise, the offsets get mixed up
        insert_anchor(end, end_anchor.node, selection_range['endOffset'])
        insert_anchor(start, start_anchor.node, selection_range['startOffset'])

        # generate range target for the note element
        target = '#range(#%s, #%s)' % (start_anchor.id, end_anchor.id)

    elif 'image_selection' in info:
        # for readux, image annotation can *only* be the page image
        # so not checking image uri
        page_width = teipage.lrx - teipage.ulx
        page_height = teipage.lry - teipage.uly

        # create a new zone for the image highlight
        image_highlight = tei.Zone(type="image-annotation-highlight")
        # image selection in annotation stored as percentages
        # convert ##% into a float that can be multiplied by page dimensions
        selection = {
            'x': float(info['image_selection']['x'].rstrip('%')) / 100,
            'y': float(info['image_selection']['y'].rstrip('%')) / 100,
            'w': float(info['image_selection']['w'].rstrip('%')) / 100,
            'h': float(info['image_selection']['h'].rstrip('%')) / 100
        }

        # convert percentages into upper left and lower right coordinates
        # relative to the page
        image_highlight.ulx = selection['x'] * float(page_width)
        image_highlight.uly = selection['y'] * float(page_height)
        image_highlight.lrx = image_highlight.ulx + (selection['w'] * page_width)
        image_highlight.lry = image_highlight.uly + (selection['h'] * page_height)

        image_highlight.id = 'highlight-%s' % annotation.id
        target = '#%s' % image_highlight.id

        teipage.node.append(image_highlight.node)

    # call annotation_to_tei and insert the resulting note into
    # the appropriate part of the document
    teinote = annotation_to_tei(annotation, teivol)
    teinote.target = target
    # append actual annotation to tei annotations div
    teivol.annotations.append(teinote)


def insert_anchor(element, anchor, offset):
    '''Insert a TEI anchor into an element at a given offset.  If
    offset is zero, anchor is inserted just before the element.  If
    offset is length of element text, anchor is inserted immediately
    after the element.

    :param element: node for the element relative to which the
        anchor will be added
    :param anchor: node for the anchor element
    :param offset: numeric offset into the element
    '''
    if offset == 0:
        # offset zero - insert directly before this element
        element.addprevious(anchor)
    elif offset >= len(element.text):
        # offset at end of this element - insert directly after
        element.addnext(anchor)
    else:
        # offset somewhere inside the text of this element
        # insert the element after the text and then break up
        # the lxml text and "tail" so that text after the offset
        # comes after the inserted anchor
        el_text = element.text
        element.insert(0, anchor)
        element.text = el_text[:offset]
        anchor.tail = el_text[offset:]


