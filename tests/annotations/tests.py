from __future__ import unicode_literals
import datetime
from decimal import Decimal
from operator import attrgetter
from django.db import connection

from django.core.exceptions import FieldError
from django.db.models import (
    Sum, Count,
    F, Value, Func,
    IntegerField, BooleanField, CharField, Q)
from django.db.models.expressions import ValueAnnotation
from django.db.models.fields import FieldDoesNotExist
from django.test import TestCase
import unittest
from django.utils import timezone

from .models import Author, Book, Store, DepartmentStore, Company, Employee, ShopUser, SpecialPrice, Product, Article, \
    ArticleTranslation


class NonAggregateAnnotationTestCase(TestCase):
    fixtures = ["annotations.json"]

    def test_basic_annotation(self):
        books = Book.objects.annotate(
            is_book=Value(1, output_field=IntegerField()))
        for book in books:
            self.assertEqual(book.is_book, 1)

    def test_basic_f_annotation(self):
        books = Book.objects.annotate(another_rating=F('rating'))
        for book in books:
            self.assertEqual(book.another_rating, book.rating)

    def test_joined_annotation(self):
        books = Book.objects.select_related('publisher').annotate(
            num_awards=F('publisher__num_awards'))
        for book in books:
            self.assertEqual(book.num_awards, book.publisher.num_awards)

    def test_annotate_with_aggregation(self):
        books = Book.objects.annotate(
            is_book=Value(1, output_field=IntegerField()),
            rating_count=Count('rating'))
        for book in books:
            self.assertEqual(book.is_book, 1)
            self.assertEqual(book.rating_count, 1)

    def test_aggregate_over_annotation(self):
        agg = Author.objects.annotate(other_age=F('age')).aggregate(otherage_sum=Sum('other_age'))
        other_agg = Author.objects.aggregate(age_sum=Sum('age'))
        self.assertEqual(agg['otherage_sum'], other_agg['age_sum'])

    def test_filter_annotation(self):
        books = Book.objects.annotate(
            is_book=Value(1, output_field=IntegerField())
        ).filter(is_book=1)
        for book in books:
            self.assertEqual(book.is_book, 1)

    def test_filter_annotation_with_f(self):
        books = Book.objects.annotate(
            other_rating=F('rating')
        ).filter(other_rating=3.5)
        for book in books:
            self.assertEqual(book.other_rating, 3.5)

    def test_filter_annotation_with_double_f(self):
        books = Book.objects.annotate(
            other_rating=F('rating')
        ).filter(other_rating=F('rating'))
        for book in books:
            self.assertEqual(book.other_rating, book.rating)

    def test_filter_agg_with_double_f(self):
        books = Book.objects.annotate(
            sum_rating=Sum('rating')
        ).filter(sum_rating=F('sum_rating'))
        for book in books:
            self.assertEqual(book.sum_rating, book.rating)

    def test_filter_wrong_annotation(self):
        with self.assertRaisesRegexp(FieldError, "Cannot resolve keyword .*"):
            list(Book.objects.annotate(
                sum_rating=Sum('rating')
            ).filter(sum_rating=F('nope')))

    def test_update_with_annotation(self):
        book_preupdate = Book.objects.get(pk=2)
        Book.objects.annotate(other_rating=F('rating') - 1).update(rating=F('other_rating'))
        book_postupdate = Book.objects.get(pk=2)
        self.assertEqual(book_preupdate.rating - 1, book_postupdate.rating)

    def test_annotation_with_m2m(self):
        books = Book.objects.annotate(author_age=F('authors__age')).filter(pk=1).order_by('author_age')
        self.assertEqual(books[0].author_age, 34)
        self.assertEqual(books[1].author_age, 35)

    def test_annotation_reverse_m2m(self):
        books = Book.objects.annotate(
            store_name=F('store__name')).filter(
            name='Practical Django Projects').order_by(
            'store_name')

        self.assertQuerysetEqual(
            books, [
                'Amazon.com',
                'Books.com',
                'Mamma and Pappa\'s Books'
            ],
            lambda b: b.store_name
        )

    def test_values_annotation(self):
        """
        Annotations can reference fields in a values clause,
        and contribute to an existing values clause.
        """
        # annotate references a field in values()
        qs = Book.objects.values('rating').annotate(other_rating=F('rating') - 1)
        book = qs.get(pk=1)
        self.assertEqual(book['rating'] - 1, book['other_rating'])

        # filter refs the annotated value
        book = qs.get(other_rating=4)
        self.assertEqual(book['other_rating'], 4)

        # can annotate an existing values with a new field
        book = qs.annotate(other_isbn=F('isbn')).get(other_rating=4)
        self.assertEqual(book['other_rating'], 4)
        self.assertEqual(book['other_isbn'], '155860191')

    def test_defer_annotation(self):
        """
        Deferred attributes can be referenced by an annotation,
        but they are not themselves deferred, and cannot be deferred.
        """
        qs = Book.objects.defer('rating').annotate(other_rating=F('rating') - 1)

        with self.assertNumQueries(2):
            book = qs.get(other_rating=4)
            self.assertEqual(book.rating, 5)
            self.assertEqual(book.other_rating, 4)

        with self.assertRaisesRegexp(FieldDoesNotExist, "\w has no field named u?'other_rating'"):
            book = qs.defer('other_rating').get(other_rating=4)

    def test_mti_annotations(self):
        """
        Fields on an inherited model can be referenced by an
        annotated field.
        """
        d = DepartmentStore.objects.create(
            name='Angus & Robinson',
            original_opening=datetime.date(2014, 3, 8),
            friday_night_closing=datetime.time(21, 00, 00),
            chain='Westfield'
        )

        books = Book.objects.filter(rating__gt=4)
        for b in books:
            d.books.add(b)

        qs = DepartmentStore.objects.annotate(
            other_name=F('name'),
            other_chain=F('chain'),
            is_open=Value(True, BooleanField()),
            book_isbn=F('books__isbn')
        ).select_related('store').order_by('book_isbn').filter(chain='Westfield')

        self.assertQuerysetEqual(
            qs, [
                ('Angus & Robinson', 'Westfield', True, '155860191'),
                ('Angus & Robinson', 'Westfield', True, '159059725')
            ],
            lambda d: (d.other_name, d.other_chain, d.is_open, d.book_isbn)
        )

    def test_column_field_ordering(self):
        """
        Test that columns are aligned in the correct order for
        resolve_columns. This test will fail on mysql if column
        ordering is out. Column fields should be aligned as:
        1. extra_select
        2. model_fields
        3. annotation_fields
        4. model_related_fields
        """
        store = Store.objects.first()
        Employee.objects.create(id=1, first_name='Max', manager=True, last_name='Paine',
                                store=store, age=23, salary=Decimal(50000.00))
        Employee.objects.create(id=2, first_name='Buffy', manager=False, last_name='Summers',
                                store=store, age=18, salary=Decimal(40000.00))

        qs = Employee.objects.extra(
            select={'random_value': '42'}
        ).select_related('store').annotate(
            annotated_value=Value(17, output_field=IntegerField())
        )

        rows = [
            (1, 'Max', True, 42, 'Paine', 23, Decimal(50000.00), store.name, 17),
            (2, 'Buffy', False, 42, 'Summers', 18, Decimal(40000.00), store.name, 17)
        ]

        self.assertQuerysetEqual(
            qs.order_by('id'), rows,
            lambda e: (
                e.id, e.first_name, e.manager, e.random_value, e.last_name, e.age,
                e.salary, e.store.name, e.annotated_value))

    def test_column_field_ordering_with_deferred(self):
        store = Store.objects.first()
        Employee.objects.create(id=1, first_name='Max', manager=True, last_name='Paine',
                                store=store, age=23, salary=Decimal(50000.00))
        Employee.objects.create(id=2, first_name='Buffy', manager=False, last_name='Summers',
                                store=store, age=18, salary=Decimal(40000.00))

        qs = Employee.objects.extra(
            select={'random_value': '42'}
        ).select_related('store').annotate(
            annotated_value=Value(17, output_field=IntegerField())
        )

        rows = [
            (1, 'Max', True, 42, 'Paine', 23, Decimal(50000.00), store.name, 17),
            (2, 'Buffy', False, 42, 'Summers', 18, Decimal(40000.00), store.name, 17)
        ]

        # and we respect deferred columns!
        self.assertQuerysetEqual(
            qs.defer('age').order_by('id'), rows,
            lambda e: (
                e.id, e.first_name, e.manager, e.random_value, e.last_name, e.age,
                e.salary, e.store.name, e.annotated_value))

    def test_custom_functions(self):
        Company(name='Apple', motto=None, ticker_name='APPL', description='Beautiful Devices').save()
        Company(name='Django Software Foundation', motto=None, ticker_name=None, description=None).save()
        Company(name='Google', motto='Do No Evil', ticker_name='GOOG', description='Internet Company').save()
        Company(name='Yahoo', motto=None, ticker_name=None, description='Internet Company').save()

        qs = Company.objects.annotate(
            tagline=Func(
                F('motto'),
                F('ticker_name'),
                F('description'),
                Value('No Tag'),
                function='COALESCE')
            ).order_by('name')

        self.assertQuerysetEqual(
            qs, [
                ('Apple', 'APPL'),
                ('Django Software Foundation', 'No Tag'),
                ('Google', 'Do No Evil'),
                ('Yahoo', 'Internet Company')
            ],
            lambda c: (c.name, c.tagline)
        )

    def test_custom_functions_can_ref_other_functions(self):
        Company(name='Apple', motto=None, ticker_name='APPL', description='Beautiful Devices').save()
        Company(name='Django Software Foundation', motto=None, ticker_name=None, description=None).save()
        Company(name='Google', motto='Do No Evil', ticker_name='GOOG', description='Internet Company').save()
        Company(name='Yahoo', motto=None, ticker_name=None, description='Internet Company').save()

        class Lower(Func):
            function = 'LOWER'

        qs = Company.objects.annotate(
            tagline=Func(
                F('motto'),
                F('ticker_name'),
                F('description'),
                Value('No Tag'),
                function='COALESCE')
        ).annotate(
            tagline_lower=Lower(F('tagline'), output_field=CharField())
        ).order_by('name')

        # LOWER function supported by:
        # oracle, postgres, mysql, sqlite, sqlserver

        self.assertQuerysetEqual(
            qs, [
                ('Apple', 'APPL'.lower()),
                ('Django Software Foundation', 'No Tag'.lower()),
                ('Google', 'Do No Evil'.lower()),
                ('Yahoo', 'Internet Company'.lower())
            ],
            lambda c: (c.name, c.tagline_lower)
        )


@unittest.skipUnless(connection.vendor == 'postgresql', 'PostgreSQL required')
class ProductTestCase(TestCase):
    def setUp(self):
        self.u1 = ShopUser.objects.create(username='tom')
        self.u2 = ShopUser.objects.create(username='brad')

        self.p1 = Product.objects.create(name='Sunflower', price=Decimal('9.99'))
        self.p2 = Product.objects.create(name='Flowers', price=Decimal('11.99'))
        self.p3 = Product.objects.create(name='Shrub', price=Decimal('31.99'))
        self.p4 = Product.objects.create(name='Bonsai', price=Decimal('121.99'))

        # Special prices for user u1
        self.sp1 = SpecialPrice.objects.create(
            product=self.p2, user=self.u1, price=Decimal('8.00'),
            valid_from=timezone.now(), valid_until=timezone.now()+datetime.timedelta(days=1)
        )
        self.sp2 = SpecialPrice.objects.create(
            product=self.p3, user=self.u1, price=Decimal('30.00'),
            valid_from=timezone.now(), valid_until=timezone.now()+datetime.timedelta(days=1)
        )

        # Special prices for user u2
        self.sp3 = SpecialPrice.objects.create(
            product=self.p2, user=self.u2, price=Decimal('9.00'),
            valid_from=timezone.now(), valid_until=timezone.now()+datetime.timedelta(days=1)
        )

    def test_sort_products_special_price_for_user(self):
        # Sorts the products according to their special price given a specific user for the join condition.
        #
        # PostgreSQL-specific note: The GREATEST and LEAST functions select the largest or smallest value from a list of
        # any number of expressions. The expressions must all be convertible to a common data type, which will be the
        # type of the result (see Section 10.5 for details). NULL values in the list are ignored. The result will be
        # NULL only if all the expressions evaluate to NULL.
        # Note that GREATEST and LEAST are not in the SQL standard, but are a common extension.
        # Some other databases make them return NULL if any argument is NULL, rather than only when all are NULL.
        qs = Product.objects.annotate(
            best_price=Func(
                F('price'),
                ValueAnnotation('specialprice__price', Q(specialprice__user=self.u1)),
                function='LEAST'
            )
        ).order_by('best_price')

        self.assertQuerysetEqual(
            qs, [
                'Flowers',
                'Sunflower',
                'Shrub',
                'Bonsai',
            ],
            attrgetter('name')
        )

        self.assertQuerysetEqual(
            qs, [
                Decimal('8.00'),
                Decimal('9.99'),
                Decimal('30.00'),
                Decimal('121.99'),
            ],
            attrgetter('best_price')
        )

    def test_multiple_join_conditions(self):
        current_time = timezone.now()+datetime.timedelta(hours=12)
        qs = Product.objects.annotate(
            best_price=Func(
                F('price'),
                ValueAnnotation(
                    'specialprice__price',
                    Q(specialprice__user=self.u1) &
                    Q(specialprice__valid_from__lte=current_time) &
                    Q(specialprice__valid_until__gte=current_time)
                ),
                function='LEAST'
            )
        ).order_by('best_price')

        self.assertQuerysetEqual(
            qs, [
                'Flowers',
                'Sunflower',
                'Shrub',
                'Bonsai',
            ],
            attrgetter('name')
        )

        self.assertQuerysetEqual(
            qs, [
                Decimal('8.00'),
                Decimal('9.99'),
                Decimal('30.00'),
                Decimal('121.99'),
            ],
            attrgetter('best_price')
        )


class ModelTranslationTestCase(TestCase):
    def setUp(self):
        self.a1 = Article.objects.create()
        self.a1_de = ArticleTranslation.objects.create(article=self.a1, lang='de', text='hallo', text2='zusammen')
        self.a1_en = ArticleTranslation.objects.create(article=self.a1, lang='en', text='hello', text2='all')

        self.a2 = Article.objects.create()
        self.a2_de = ArticleTranslation.objects.create(article=self.a2, lang='de', text='guten', text2='abend')
        self.a2_en = ArticleTranslation.objects.create(article=self.a2, lang='en', text='good', text2='evening')

    def test_language_data_is_loaded(self):
        qs = Article.objects.annotate(
            text=ValueAnnotation('articletranslation__text', Q(articletranslation__lang='de')),
            text2=ValueAnnotation('articletranslation__text2', Q(articletranslation__lang='de')),
        ).order_by('pk')

        print qs.query

        self.assertQuerysetEqual(
            qs, [
                'hallo',
                'guten',
            ],
            attrgetter('text')
        )

        self.assertQuerysetEqual(
            qs, [
                'zusammen',
                'abend',
            ],
            attrgetter('text2')
        )